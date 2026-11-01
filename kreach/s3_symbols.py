"""S3 -- Symbol references.

Find which objects reference which symbols, and which objects provide them.

  Input:  build trees from a small set of wide configurations
          (allmodconfig, allyesconfig) for this architecture.
  Method: read symbols from the compiled objects (nm/ELF), not from C source.
          The compiler has already resolved macros, inlining and dead branches
          for that configuration, which source parsing gets wrong.
  Output: s3/symbols.json.gz -- (caller object, symbol) references,
          symbol -> provider objects, module map, weak/absolute symbols.

Providers outside the built subtree.  A build restricted to one subsystem
compiles the callers but not every provider, so the exported-symbol side is
completed by a whole-tree scan for EXPORT_SYMBOL*.  That macro takes the
symbol name as a literal token at file scope, so the scan is reliable for
*locating* a provider even though it cannot tell us anything about
configuration.  References always come from binaries, as the spec requires.
"""

import os
import re
import subprocess
import time

from kreach.config import Context
from kreach.util import get_logger, human_time, read_json, run, write_json

log = get_logger("s3")

EXPORT_RE = re.compile(
    r"\bEXPORT_SYMBOL(?:_GPL|_NS|_NS_GPL|_GPL_FOR_MODULES|_FOR_MODULES)?\s*\(\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*[,)]")

# nm symbol types we care about
NM_UNDEFINED = {"U"}
NM_WEAK = {"w", "W", "v", "V"}
NM_DEFINED_GLOBAL = {"T", "D", "B", "R", "A", "G", "S", "C", "i", "I"}

# References every object makes into the compiler/runtime rather than into
# other kernel code; they are never a Kconfig reachability problem.
IGNORED_SYMBOL_PREFIXES = (
    "__sanitizer_cov", "__asan_", "__kasan_", "__msan_", "__ubsan_",
    "__gcov_", "__stack_chk", "__fentry__", "__x86_return_thunk",
    "__x86_indirect_thunk", "__SCT__", "__cfi_", "__kcfi_", "__tsan_",
    "__mcount", "mcount", "__stack_chk_guard",
)
IGNORED_SYMBOLS = {"memcpy", "memset", "memmove", "memcmp", "__memcpy",
                   "__memset", "__memmove", "strlen", "strcmp", "_mcount"}


def _is_interesting(sym: str) -> bool:
    if sym in IGNORED_SYMBOLS:
        return False
    return not sym.startswith(IGNORED_SYMBOL_PREFIXES)


def make(ctx: Context, builddir: str, targets: list[str], jobs: int | None = None,
         extra_env: dict | None = None, check: bool = True, timeout: int | None = None,
         logfile: str | None = None, make_vars: dict | None = None):
    """Invoke the kernel's make with O=<builddir>.

    `make_vars` are passed as make variables (``VAR=value`` on the command
    line) rather than environment variables, which is how the kernel build
    expects overrides such as ``CC``.
    """
    env = ctx.make_env()
    if extra_env:
        env.update(extra_env)
    cmd = ["make", "O=%s" % os.path.abspath(builddir),
           "-j%d" % (jobs or ctx.jobs)]
    for key, value in sorted((make_vars or {}).items()):
        cmd.append("%s=%s" % (key, value))
    cmd += list(targets)
    t0 = time.time()
    log.info("make %s (O=%s, -j%d)", " ".join(targets), builddir, jobs or ctx.jobs)
    proc = run(cmd, cwd=ctx.ksrc, env=env, check=False, timeout=timeout, log=log)
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        with open(logfile, "a") as fh:
            fh.write("$ %s\n" % " ".join(cmd))
            fh.write(proc.stdout or "")
            fh.write(proc.stderr or "")
    log.info("  -> rc=%d in %s", proc.returncode, human_time(time.time() - t0))
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-3000:]
        raise RuntimeError("make %s failed (rc=%d):\n%s"
                           % (" ".join(targets), proc.returncode, tail))
    return proc


def build_wide(ctx: Context, config: str, subdirs: list[str] | None = None,
               force: bool = False) -> str:
    """Configure and build one wide configuration, optionally subtree-only."""
    bdir = ctx.s3_build_dir(config)
    stamp = os.path.join(bdir, ".kreach-built")
    if os.path.exists(stamp) and not force:
        log.info("reusing S3 build in %s", bdir)
        return bdir
    logfile = os.path.join(bdir, "build.log")
    make(ctx, bdir, [config], logfile=logfile)
    # Modules must be possible for allmodconfig to mean anything.
    targets = list(subdirs) if subdirs else ["vmlinux", "modules"]
    if subdirs:
        # Kbuild needs the generated headers before building a subtree.
        make(ctx, bdir, ["prepare", "scripts"], logfile=logfile)
        targets = [d if d.endswith("/") else d + "/" for d in subdirs]
    make(ctx, bdir, targets, logfile=logfile, check=False)
    with open(stamp, "w") as fh:
        fh.write("%s\n" % config)
    return bdir


def _find_objects(builddir: str, subdirs: list[str] | None) -> list[str]:
    roots = [os.path.join(builddir, d) for d in subdirs] if subdirs else [builddir]
    objs = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("scripts", "tools", ".tmp_versions")]
            for fn in filenames:
                if not fn.endswith(".o"):
                    continue
                if fn.endswith(".mod.o"):
                    continue
                objs.append(os.path.join(dirpath, fn))
    return sorted(objs)


def _nm_batch(paths: list[str], chunk: int = 300):
    """Yield (path, symtype, symbol) for a list of object files."""
    for i in range(0, len(paths), chunk):
        batch = paths[i:i + chunk]
        proc = subprocess.run(["nm", "--no-sort", "--print-file-name"] + batch,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, errors="replace")
        for line in proc.stdout.splitlines():
            # "path/foo.o:                 U bar_register"
            path, _, rest = line.partition(":")
            rest = rest.strip()
            if not rest:
                continue
            parts = rest.split()
            if len(parts) == 2:
                symtype, name = parts
            elif len(parts) == 3:
                symtype, name = parts[1], parts[2]
            else:
                continue
            yield path, symtype, name


def scan_exports(ksrc: str) -> dict[str, list[str]]:
    """Whole-tree EXPORT_SYMBOL* scan: symbol -> candidate provider objects."""
    t0 = time.time()
    out: dict[str, list[str]] = {}
    nfiles = 0
    for dirpath, dirnames, filenames in os.walk(ksrc):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "Documentation", "tools", "samples",
                                    "scripts", "usr", "LICENSES")]
        for fn in filenames:
            if not (fn.endswith(".c") or fn.endswith(".S")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                text = open(path, errors="replace").read()
            except OSError:
                continue
            nfiles += 1
            if "EXPORT_SYMBOL" not in text:
                continue
            rel = os.path.relpath(path, ksrc)
            obj = os.path.splitext(rel)[0] + ".o"
            for m in EXPORT_RE.finditer(text):
                out.setdefault(m.group(1), []).append(obj)
    log.info("EXPORT_SYMBOL scan: %d symbols from %d source files in %s",
             len(out), nfiles, human_time(time.time() - t0))
    return out


def read_module_map(builddir: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """From the build's .mod files: object -> module, and composite -> parts.

    A module built from several sources is linked into one object -- for
    instance ``sound/soc/snd-soc-core.o`` from ``soc-core.o``, ``soc-ac97.o``
    and a dozen others.  ``nm`` on that linked object reports the union of its
    parts' undefined symbols, so a reference made by one part looks like a
    reference made by the whole module.  That matters because the composite
    carries only the module's own Kbuild condition while each part may be
    guarded by more:

        snd-soc-core.o   SND_SOC and SND
        soc-ac97.o       SND_SOC and SND and SND_SOC_AC97_BUS

    S4 needs the narrower one, so the composites are reported here and it
    prefers their parts.
    """
    mapping: dict[str, str] = {}
    composites: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(builddir):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            if not fn.endswith(".mod"):
                continue
            modobj = os.path.normpath(
                os.path.relpath(os.path.join(dirpath, fn[:-4] + ".o"), builddir))
            modname = os.path.basename(fn)[:-4]
            try:
                parts = open(os.path.join(dirpath, fn), errors="replace").read().split()
            except OSError:
                continue
            objs = [os.path.normpath(p) for p in parts if p.endswith(".o")]
            for p in objs:
                mapping[p] = modname
            mapping.setdefault(modobj, modname)
            others = [p for p in objs if p != modobj]
            if others:
                composites[modobj] = sorted(others)
    return mapping, composites


def read_symvers(builddir: str) -> dict[str, dict]:
    """exported symbol -> {module, license} from Module.symvers."""
    path = os.path.join(builddir, "Module.symvers")
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) < 3:
                continue
            sym, mod = fields[1], fields[2]
            out[sym] = {"module": mod, "license": fields[3] if len(fields) > 3 else ""}
    return out


def run_s3(ctx: Context, configs=("allmodconfig",), subdirs: list[str] | None = None,
           force: bool = False, skip_build: bool = False) -> dict:
    t0 = time.time()
    references: dict[str, list[str]] = {}   # caller object -> [symbols]
    provided: dict[str, list[str]] = {}     # symbol -> [provider objects]
    weak: dict[str, list[str]] = {}
    module_map: dict[str, str] = {}
    composites: dict[str, list[str]] = {}
    symvers: dict[str, dict] = {}
    builds: list[str] = []

    for config in configs:
        if skip_build:
            bdir = ctx.s3_build_dir(config)
            if not os.path.isdir(bdir):
                log.warning("no existing build for %s, skipping", config)
                continue
        else:
            bdir = build_wide(ctx, config, subdirs=subdirs, force=force)
        builds.append(config)
        objs = _find_objects(bdir, subdirs)
        log.info("%s: %d object files in %s", config, len(objs), bdir)
        for path, symtype, name in _nm_batch(objs):
            rel = os.path.normpath(os.path.relpath(path, bdir))
            if symtype in NM_UNDEFINED:
                if _is_interesting(name):
                    references.setdefault(rel, [])
                    if name not in references[rel]:
                        references[rel].append(name)
            elif symtype in NM_WEAK:
                weak.setdefault(name, [])
                if rel not in weak[name]:
                    weak[name].append(rel)
            elif symtype in NM_DEFINED_GLOBAL:
                provided.setdefault(name, [])
                if rel not in provided[name]:
                    provided[name].append(rel)
        mm, comp = read_module_map(bdir)
        module_map.update(mm)
        composites.update(comp)
        symvers.update(read_symvers(bdir))

    exports = scan_exports(ctx.ksrc)
    # Complete the provider side with the source scan for symbols the build
    # did not compile.
    added = 0
    for sym, objs in exports.items():
        cur = provided.setdefault(sym, [])
        for obj in objs:
            if obj not in cur:
                cur.append(obj)
                added += 1

    blob = {
        "configs": builds,
        "subdirs": subdirs,
        "references": references,
        "providers": provided,
        "weak": weak,
        "exports": exports,
        "module_map": module_map,
        "composites": composites,
        "symvers": symvers,
    }
    write_json(ctx.symbols_file, blob)
    nrefs = sum(len(v) for v in references.values())
    ctx.write_meta({"s3": {
        "configs": builds,
        "caller_objects": len(references),
        "references": nrefs,
        "provided_symbols": len(provided),
        "exported_symbols": len(exports),
        "weak_symbols": len(weak),
        "composite_objects": len(composites),
        "providers_from_source_scan": added,
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S3 complete: %d references from %d objects, %d provided symbols, "
             "%d composite objects, in %s", nrefs, len(references), len(provided),
             len(composites), human_time(time.time() - t0))
    return blob


def load_s3(ctx: Context) -> dict:
    if not os.path.exists(ctx.symbols_file):
        raise FileNotFoundError(
            "no S3 table at %s -- run 'kreach s3' first" % ctx.symbols_file)
    return read_json(ctx.symbols_file)
