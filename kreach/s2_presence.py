"""S2 -- Kbuild presence conditions.

Compute, for every object file, the condition under which it is built into
vmlinux and the condition under which it is built at all.

  Input:  source tree, S1 symbol types.
  Output: s2/presence.json.gz -- object -> (builtin condition, available
          condition, module name), as SMT-LIB strings.  The spec states the
          output as (condition for y, condition for m, module name); the m
          condition is ``available and not builtin``, which `PresenceTable.module`
          returns.  Storing (y, available) rather than (y, m) is what the
          queries use directly and keeps the two independent.
          s2/unmodelled.json  -- Makefile constructs we could not model.

Reuse: kmax computes a per-object presence condition already, so kreach does
not parse Makefiles to find objects.  But kmax's condition is over *enabled*
variables -- it says the object is compiled, not at which level, because
``obj-$(CONFIG_X) += foo.o`` puts foo.o somewhere whenever X is y or m.  The
level split is kreach's job:

    available(o) = kmax's condition, unchanged
    builtin(o)   = the same formula with each selector variable X replaced by
                   X==y, since the object reaches obj-y only when its
                   selectors are y

Three kinds of Makefile test, not two:

  ``obj-$(CONFIG_X) += foo.o``   selector: foo.o is compiled when X is y or m,
                                 and built in only when X is y -- so X is
                                 relabelled in `builtin` and left alone in
                                 `available`
  ``ifdef CONFIG_X``             true for y and m, so X keeps its enabled
  ``ifneq ($(CONFIG_X),)``       reading in both
  ``ifeq ($(CONFIG_X),y)``       exact y: the object is not compiled *at all*
                                 unless X is y, so X is relabelled in
                                 `available` too

All three readings are per-object, not global.  ``sound/soc/Makefile`` has

    ifneq ($(CONFIG_SND_SOC_ACPI),)
    obj-$(CONFIG_SND_SOC_ACPI) += soc-acpi.o
    endif

where one symbol is a guard and a selector on adjacent lines.  A global guard
set would keep the enabled reading and predict `y` for an object that is built
as a module.  Where a symbol is a selector for an object, the selector reading
wins -- it is the stronger of the two and both apply -- so S2 records which
symbols actually select each object and subtracts those from the guard set.

For a part of a composite the selector is on the composite, not the part:

    ifneq ($(CONFIG_SND_SOC_ACPI),)
    snd-soc-acpi-y := soc-acpi.o
    endif
    obj-$(CONFIG_SND_SOC_ACPI) += snd-soc-acpi.o

so ``soc-acpi.o`` inherits its composite's selectors.

The third kind is why ``sound/last.o`` exists:

    ifeq ($(CONFIG_SND),y)
      obj-y += last.o
    endif

With ``CONFIG_SND=m`` that object is not built, while every other object under
``sound/`` is built as a module.  Treating the test as a plain selector
predicts `m` and is wrong.  The symbols cannot be collected globally -- the
same ``CONFIG_SND`` is an ordinary selector two lines above -- so S2 tracks
which objects and directories actually sit inside such a block.

``ifeq ($(CONFIG_X),m)`` cannot be expressed by this relabelling at all and is
recorded in unmodelled.json.

The S2 acceptance test (`kreach test s2`) checks the result against real
builds, which is what the spec asks for: any mismatch is a bug in S2.
"""

import os
import re
import time

import z3

from kreach import formula as F
from kreach.config import Context
from kreach.util import get_logger, human_time, read_json, write_json

log = get_logger("s2")

# ifdef / ifndef CONFIG_X            -> true for y and m   (enabled reading)
# ifeq/ifneq ($(CONFIG_X),)          -> emptiness test     (enabled reading)
# ifeq/ifneq ($(CONFIG_X),y)         -> exact y            (selector reading)
# ifeq/ifneq ($(CONFIG_X),m)         -> exact m            (not expressible)
RE_IFDEF = re.compile(r"^\s*(ifdef|ifndef)\s+(CONFIG_[A-Za-z0-9_]+)")
RE_IFEQ = re.compile(
    r"^\s*(ifeq|ifneq)\s*\(\s*\$\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)\s*,\s*([^)]*)\)")


class PresenceTable:
    """object path -> presence conditions, with lazy z3 parsing."""

    def __init__(self, data: dict, guards: set[str], exact_m: set[str],
                 undefined: set[str] | None = None):
        self._data = data
        self.guards = set(guards)
        self.exact_m = set(exact_m)
        # CONFIG symbols Kbuild refers to that this architecture's Kconfig
        # never declares.  They can only be n; see
        # formula.undefined_symbols_off for why that has to be said.
        self.undefined = set(undefined or ())
        self._cache: dict[str, tuple] = {}

    def __contains__(self, obj: str) -> bool:
        return obj in self._data

    def __len__(self) -> int:
        return len(self._data)

    def objects(self):
        return self._data.keys()

    def module_of(self, obj: str) -> str | None:
        rec = self._data.get(obj)
        return rec.get("module") if rec else None

    def own_symbols(self, obj: str) -> list[str]:
        rec = self._data.get(obj) or {}
        return list(rec.get("own_symbols") or ())

    def all_symbols(self, obj: str) -> list[str]:
        rec = self._data.get(obj) or {}
        return list(rec.get("all_symbols") or ())

    def conditions(self, obj: str) -> tuple:
        """(builtin, available) as z3 expressions."""
        if obj in self._cache:
            return self._cache[obj]
        rec = self._data[obj]
        avail = F.conj(z3.parse_smt2_string(rec["available"]))
        builtin = F.conj(z3.parse_smt2_string(rec["builtin"]))
        self._cache[obj] = (builtin, avail)
        return self._cache[obj]

    def available(self, obj: str):
        return self.conditions(obj)[1]

    def builtin(self, obj: str):
        """The condition for level y: the object is linked into vmlinux."""
        return self.conditions(obj)[0]

    def module(self, obj: str):
        """The condition for level m: built, but not into vmlinux.

        The spec states S2's output as (condition for y, condition for m,
        module name).  The table stores the y condition and the "built at all"
        condition instead, because that is the pair the queries use directly --
        the m condition is ``available and not builtin`` and is derived here so
        the stated output is available as such.
        """
        builtin, avail = self.conditions(obj)
        return z3.And(avail, z3.Not(builtin))

    def raw(self) -> dict:
        return self._data


def _smt2(expr) -> str:
    s = z3.Solver()
    s.add(expr)
    return s.to_smt2()


def scan_makefile_guards(ksrc: str, subdirs=None) -> tuple[set[str], set[str], list]:
    """Collect symbols used in Makefile conditionals.

    Returns (enabled_reading, exact_m, notes).  `enabled_reading` must not be
    relabelled to ==y; `exact_m` cannot be modelled by the relabelling at all.
    """
    enabled: set[str] = set()
    exact_m: set[str] = set()
    notes: list[dict] = []
    roots = [os.path.join(ksrc, d) for d in (subdirs or ["."])]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "Documentation")]
            for fn in filenames:
                if fn != "Makefile" and fn != "Kbuild" and not fn.startswith("Makefile."):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ksrc)
                try:
                    with open(path, errors="replace") as fh:
                        lines = fh.readlines()
                except OSError:
                    continue
                for lineno, line in enumerate(lines, 1):
                    m = RE_IFDEF.match(line)
                    if m:
                        enabled.add(m.group(2))
                        continue
                    m = RE_IFEQ.match(line)
                    if m:
                        sym, val = m.group(2), m.group(3).strip()
                        if val == "":
                            enabled.add(sym)
                        elif val == "y":
                            pass  # exact-y: the relabelling is already right
                        elif val == "m":
                            exact_m.add(sym)
                            notes.append({
                                "kind": "ifeq-m",
                                "file": rel, "line": lineno,
                                "symbol": sym,
                                "text": line.strip(),
                            })
                        else:
                            enabled.add(sym)
                            notes.append({
                                "kind": "ifeq-other",
                                "file": rel, "line": lineno,
                                "symbol": sym, "value": val,
                                "text": line.strip(),
                            })
    return enabled, exact_m, notes


RE_IF_ANY = re.compile(r"^\s*(if(?:def|ndef|eq|neq))\b")
RE_ELSE = re.compile(r"^\s*else\b")
RE_ENDIF = re.compile(r"^\s*endif\b")
RE_OBJ_ASSIGN = re.compile(r"^\s*[A-Za-z0-9_\-]+-(?:y|m|objs|\$\([^)]*\))\s*[+:]?=\s*(.*)$")


RE_OBJ_SELECTOR = re.compile(
    r"^\s*[A-Za-z0-9_\-]+-\$\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)\s*[+:]?=\s*(.*)$")


def scan_selector_scopes(ksrc: str, subdirs) -> tuple[dict, dict]:
    """Which symbols select each object, from ``obj-$(CONFIG_X) += foo.o``.

    Returns ({object: {symbols}}, {directory: {symbols}}).  A symbol in this
    position is an exact-y test for whether the object reaches ``obj-y``, so
    it must be relabelled in `builtin` even when the same symbol is used as an
    ``ifdef`` guard somewhere -- which happens on adjacent lines in
    ``sound/soc/Makefile``.
    """
    objects: dict[str, set] = {}
    dirs: dict[str, set] = {}
    for sub in subdirs:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ksrc, sub)):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                if fn != "Makefile" and fn != "Kbuild" and not fn.startswith("Makefile."):
                    continue
                reldir = os.path.relpath(dirpath, ksrc)
                try:
                    text = open(os.path.join(dirpath, fn), errors="replace").read()
                except OSError:
                    continue
                for line in text.replace("\\\n", " ").splitlines():
                    m = RE_OBJ_SELECTOR.match(line)
                    if not m:
                        continue
                    sym = m.group(1)
                    for tok in m.group(2).split():
                        if tok.endswith(".o"):
                            key = os.path.normpath(os.path.join(reldir, tok))
                            objects.setdefault(key, set()).add(sym)
                        elif tok.endswith("/"):
                            key = os.path.normpath(os.path.join(reldir, tok)) + "/"
                            dirs.setdefault(key, set()).add(sym)
    return objects, dirs


def scan_exact_y_scopes(ksrc: str, subdirs) -> tuple[dict, dict]:
    """Objects and directories that sit inside an ``ifeq ($(CONFIG_X),y)``.

    Returns ({object: {symbols}}, {directory: {symbols}}).  A directory entry
    applies to everything beneath it, because ``obj-y += dir/`` inside such a
    block makes the whole subtree conditional on the exact-y test.
    """
    objects: dict[str, set] = {}
    dirs: dict[str, set] = {}
    for sub in subdirs:
        root = os.path.join(ksrc, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                if fn != "Makefile" and fn != "Kbuild" and not fn.startswith("Makefile."):
                    continue
                reldir = os.path.relpath(dirpath, ksrc)
                try:
                    text = open(os.path.join(dirpath, fn), errors="replace").read()
                except OSError:
                    continue
                text = text.replace("\\\n", " ")
                stack: list[set] = []
                for line in text.splitlines():
                    if RE_ENDIF.match(line):
                        if stack:
                            stack.pop()
                        continue
                    if RE_ELSE.match(line):
                        # The else branch is the negation; the exact-y test
                        # does not hold there.
                        if stack:
                            stack[-1] = set()
                        continue
                    m = RE_IF_ANY.match(line)
                    if m:
                        syms: set = set()
                        eq = RE_IFEQ.match(line)
                        if eq and eq.group(1) == "ifeq" and eq.group(3).strip() == "y":
                            syms = {eq.group(2)}
                        stack.append(syms)
                        continue
                    active: set = set()
                    for frame in stack:
                        active |= frame
                    if not active:
                        continue
                    am = RE_OBJ_ASSIGN.match(line)
                    if not am:
                        continue
                    for tok in am.group(1).split():
                        if tok.endswith(".o"):
                            key = os.path.normpath(os.path.join(reldir, tok))
                            objects.setdefault(key, set()).update(active)
                        elif tok.endswith("/"):
                            key = os.path.normpath(os.path.join(reldir, tok)) + "/"
                            dirs.setdefault(key, set()).update(active)
    return objects, dirs


def exact_y_symbols_for(obj: str, objects: dict, dirs: dict) -> set:
    """The exact-y symbols that apply to one object."""
    out = set(objects.get(obj, ()))
    parts = obj.split("/")
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i]) + "/"
        if prefix in dirs:
            out |= dirs[prefix]
    return out


def run_kmax(ctx: Context, subdirs: list[str], force: bool = False) -> dict:
    """Run kmaxall over `subdirs`, producing the per-object condition map."""
    import pickle

    if os.path.exists(ctx.kmax_file) and not force:
        log.info("reusing kmax formulas in %s", ctx.kmax_file)
        with open(ctx.kmax_file, "rb") as fh:
            return pickle.load(fh)

    t0 = time.time()
    log.info("running kmaxall over %s (this walks every Makefile below them)",
             " ".join(subdirs))
    # kmaxall writes a binary pickle to stdout, so capture it as bytes.
    import subprocess
    cmd = ["kmaxall", "-z"] + list(subdirs)
    log.debug("run: %s (cwd=%s)", " ".join(cmd), ctx.ksrc)
    proc = subprocess.run(cmd, cwd=ctx.ksrc, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        log.warning("kmaxall returned %d; stderr tail:\n%s",
                    proc.returncode, proc.stderr.decode(errors="replace")[-2000:])
    formulas = pickle.loads(proc.stdout)
    os.makedirs(os.path.dirname(ctx.kmax_file), exist_ok=True)
    with open(ctx.kmax_file, "wb") as fh:
        pickle.dump(formulas, fh, 0)
    log.info("kmax: %d paths in %s -> %s",
             len(formulas), human_time(time.time() - t0), ctx.kmax_file)
    return formulas


def _module_name_hints(ksrc: str, subdirs: list[str]) -> dict[str, str]:
    """Best-effort object -> module name from composite Kbuild assignments.

    S3 overwrites these with the exact mapping from a real build's .mod files;
    this is only here so S2's table is usable on its own.
    """
    hints: dict[str, str] = {}
    comp_re = re.compile(r"^\s*([A-Za-z0-9_\-]+)-(?:y|m|objs|\$\([^)]*\))\s*[+:]?=\s*(.*)$")
    # Kbuild's own list variables use the same shape as a composite module
    # ("obj-$(CONFIG_X) += foo.o"), so they have to be excluded by name.
    kbuild_vars = {
        "obj", "lib", "subdir", "hostprogs", "hostcxxlibs", "userprogs",
        "always", "targets", "extra", "clean-files", "clean", "generic",
        "mandatory", "no-clean-files", "ldflags", "ccflags", "asflags",
        "cflags", "aflags", "subdir-ccflags", "subdir-asflags", "modname",
    }
    for sub in subdirs:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ksrc, sub)):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                if fn not in ("Makefile", "Kbuild"):
                    continue
                reldir = os.path.relpath(dirpath, ksrc)
                try:
                    text = open(os.path.join(dirpath, fn), errors="replace").read()
                except OSError:
                    continue
                text = text.replace("\\\n", " ")
                for line in text.splitlines():
                    m = comp_re.match(line)
                    if not m:
                        continue
                    base, rhs = m.group(1), m.group(2)
                    if base in kbuild_vars:
                        continue
                    for tok in rhs.split():
                        if tok.endswith(".o"):
                            obj = os.path.normpath(os.path.join(reldir, tok))
                            hints[obj] = base
    return hints


def run_s2(ctx: Context, subdirs: list[str], kconfig_types: dict,
           force: bool = False) -> PresenceTable:
    t0 = time.time()
    formulas = run_kmax(ctx, subdirs, force=force)

    guards, exact_m, notes = scan_makefile_guards(ctx.ksrc, subdirs)
    log.info("Makefile conditionals: %d symbols keep the enabled reading, "
             "%d use an exact-m test", len(guards), len(exact_m))

    hints = _module_name_hints(ctx.ksrc, subdirs)
    exact_y_objs, exact_y_dirs = scan_exact_y_scopes(ctx.ksrc, subdirs)
    log.info("exact-y blocks: %d objects and %d directories are only compiled "
             "when a symbol is y", len(exact_y_objs), len(exact_y_dirs))
    sel_objs, sel_dirs = scan_selector_scopes(ctx.ksrc, subdirs)
    log.info("selectors: %d objects and %d directories are controlled by an "
             "obj-$(CONFIG_X) assignment", len(sel_objs), len(sel_dirs))

    from kmax.common import get_kmax_constraints

    data: dict[str, dict] = {}
    skipped = 0
    tristate = {s for s, t in kconfig_types.items() if t == "tristate"}
    # Only tristate symbols have a y/m distinction worth relabelling; a bool
    # symbol's enabled variable already means y.
    relabel_skip = set(guards) | {s for s in kconfig_types if s not in tristate}

    for path in sorted(formulas):
        if not path.endswith(".o"):
            continue
        cons = get_kmax_constraints(formulas, path)
        if cons is None:
            skipped += 1
            continue
        avail_raw = F.conj(cons)
        # An object inside an "ifeq ($(CONFIG_X),y)" block is not compiled at
        # all unless X is y, so X is relabelled in the available condition too.
        exact_y = exact_y_symbols_for(path, exact_y_objs, exact_y_dirs) & tristate
        if exact_y:
            syms = F.collect_config_symbols([avail_raw])
            avail = F.relabel_builtin(avail_raw, skip=syms - exact_y)
        else:
            avail = avail_raw
        # A symbol that selects this object is an exact-y test for it, even
        # if the same symbol guards an ifdef elsewhere, so it must not be
        # protected by the guard set here.
        selectors = exact_y_symbols_for(path, sel_objs, sel_dirs)
        # A part of a composite is selected through the composite's own
        # obj-$(CONFIG_X) line, not its own.
        composite = hints.get(path)
        if composite:
            comp_obj = os.path.normpath(
                os.path.join(os.path.dirname(path), composite + ".o"))
            if comp_obj != path:
                selectors |= exact_y_symbols_for(comp_obj, sel_objs, sel_dirs)
        builtin = F.relabel_builtin(avail, skip=relabel_skip - selectors)
        # The unit's own Kbuild line, without the conditions inherited from the
        # directories above it.  S4 names the caller/provider Kconfig option
        # from this, since the inherited symbols are usually just the subsystem
        # menu above the option we care about.
        own = F.conj(z3.parse_smt2_string(formulas[path]))
        data[path] = {
            "available": _smt2(avail),
            "builtin": _smt2(builtin),
            "own_symbols": sorted(F.collect_config_symbols([own])),
            "all_symbols": sorted(F.collect_config_symbols([avail])),
            "exact_y_symbols": sorted(exact_y),
            "selector_symbols": sorted(selectors & tristate),
            "module": hints.get(path),
        }

    # Symbols Kbuild uses that this architecture's Kconfig does not declare.
    used: set[str] = set()
    for rec in data.values():
        used.update(rec.get("all_symbols") or ())
        used.update(rec.get("own_symbols") or ())
    undefined = {s for s in used if s not in kconfig_types}
    if undefined:
        log.info("%d Kbuild symbols are not declared by this architecture "
                 "(they can only be n): e.g. %s", len(undefined),
                 ", ".join(sorted(undefined)[:5]))

    write_json(ctx.presence_file, {
        "objects": data,
        "guard_symbols": sorted(guards),
        "exact_m_symbols": sorted(exact_m),
        "undefined_symbols": sorted(undefined),
        "subdirs": subdirs,
    })
    write_json(ctx.unmodelled_file, {
        "notes": notes,
        "objects_without_formula": skipped,
    })
    ctx.write_meta({"s2": {
        "objects": len(data),
        "guard_symbols": len(guards),
        "exact_m_symbols": len(exact_m),
        "undefined_symbols": len(undefined),
        "exact_y_objects": len(exact_y_objs),
        "exact_y_dirs": len(exact_y_dirs),
        "selector_objects": len(sel_objs),
        "unmodelled_notes": len(notes),
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S2 complete: %d objects, %d unmodelled notes, in %s",
             len(data), len(notes), human_time(time.time() - t0))
    return PresenceTable(data, guards, exact_m, undefined)


def load_s2(ctx: Context) -> PresenceTable:
    if not os.path.exists(ctx.presence_file):
        raise FileNotFoundError(
            "no S2 table at %s -- run 'kreach s2' first" % ctx.presence_file)
    blob = read_json(ctx.presence_file)
    return PresenceTable(blob["objects"], set(blob.get("guard_symbols", ())),
                         set(blob.get("exact_m_symbols", ())),
                         set(blob.get("undefined_symbols", ())))


def apply_module_map(ctx: Context, table: PresenceTable, module_map: dict) -> None:
    """Fill in exact module names harvested from a real build (S3)."""
    changed = 0
    for obj, mod in module_map.items():
        rec = table.raw().get(obj)
        if rec is not None and rec.get("module") != mod:
            rec["module"] = mod
            changed += 1
    if changed:
        blob = read_json(ctx.presence_file)
        blob["objects"] = table.raw()
        write_json(ctx.presence_file, blob)
        log.info("module names: %d objects updated from build data", changed)
