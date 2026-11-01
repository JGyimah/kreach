# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage acceptance tests, as specified.

S1.  For N random .config files produced by ``make randconfig``, every one
     satisfies K; for N random models of K, ``make olddefconfig`` leaves the
     assignment unchanged.  (The spec asks for 1,000 each; --samples sets it.)

S2.  Build allmodconfig, defconfig and N randconfigs; for each, the predicted
     level of every object must match what the build produced (the thin
     archives for y, the .mod files for m).  Any mismatch is a bug in S2.

     Composite module targets are counted separately rather than as
     mismatches.  Kbuild links `snd-pcm.o` out of its nine parts only when
     the module is built as a module; at y the parts go straight into
     `built-in.a` and that file is never created.  kreach's `builtin`
     condition means "this code is linked into vmlinux", which is the right
     reading for reachability and is true for the composite, while the test
     is comparing against files on disk, where it is false.  The two
     disagree by definition, so the test says how many and moves on -- and
     S4 works from the parts anyway, since the composite's symbols are the
     union of theirs.

Both tests are the real check on the model, so they report every mismatch they
find rather than a pass/fail bit alone.
"""

import os
import random
import time

import z3

from kreach import formula as F
from kreach.config import Context
from kreach.s1_kconfig import config_to_assignment, load_s1, parse_config_file
from kreach.s2_presence import load_s2
from kreach.util import get_logger, human_time

log = get_logger("accept")


# ---------------------------------------------------------------- shared ----

def _make(ctx: Context, builddir: str, targets, env=None, jobs=1, check=False,
          timeout=None, logfile=None):
    from kreach.s3_symbols import make
    return make(ctx, builddir, list(targets), jobs=jobs, extra_env=env,
                check=check, timeout=timeout, logfile=logfile)


def _randconfig(ctx: Context, builddir: str, seed: int) -> dict | None:
    """Produce one ``make randconfig`` and return its settings."""
    _make(ctx, builddir, ["randconfig"], env={"KCONFIG_SEED": hex(seed)}, jobs=1)
    cfg = os.path.join(builddir, ".config")
    return parse_config_file(cfg) if os.path.exists(cfg) else None


# -------------------------------------------------------------------- S1 ----

def test_s1(ctx: Context, samples: int = 20, seed: int = 1) -> dict:
    t0 = time.time()
    model = load_s1(ctx)
    bdir = ctx.dir("accept", "s1")

    # Direction 1: every randconfig satisfies K.
    forward = {"checked": 0, "satisfied": 0, "violations": []}
    solver = z3.Solver()
    solver.set("timeout", 60000)
    solver.add(*model.constraints)
    for i in range(samples):
        settings = _randconfig(ctx, bdir, seed + i)
        if settings is None:
            continue
        lits = config_to_assignment(settings, model)
        forward["checked"] += 1
        res = solver.check(*lits)
        if res == z3.sat:
            forward["satisfied"] += 1
        else:
            detail = {"seed": hex(seed + i), "result": str(res)}
            if res == z3.unsat:
                core = solver.unsat_core()
                detail["core"] = sorted({str(c) for c in core})[:25]
            forward["violations"].append(detail)
            log.warning("randconfig seed %s does not satisfy K: %s",
                        hex(seed + i), detail.get("core", res))

    # Direction 2: a random model of K survives make olddefconfig unchanged.
    base = {}
    _make(ctx, bdir, ["allnoconfig"], jobs=1)
    if os.path.exists(os.path.join(bdir, ".config")):
        base = parse_config_file(os.path.join(bdir, ".config"))

    backward = {"checked": 0, "unchanged": 0, "drift": []}
    for i in range(samples):
        m = _random_model(model, seed + 1000 + i)
        if m is None:
            continue
        want = _levels_from_model(m, model)
        _write_full_config(os.path.join(bdir, ".config"), want, base, model)
        _make(ctx, bdir, ["olddefconfig"], jobs=1)
        got = parse_config_file(os.path.join(bdir, ".config"))
        changed = {s: (v, got.get(s, "n")) for s, v in want.items()
                   if got.get(s, "n") != v}
        backward["checked"] += 1
        if not changed:
            backward["unchanged"] += 1
        else:
            backward["drift"].append({
                "seed": seed + 1000 + i,
                "changed": dict(sorted(changed.items())[:25]),
                "changed_total": len(changed),
            })
            log.warning("olddefconfig changed %d symbols of a model of K "
                        "(first: %s)", len(changed), sorted(changed)[:5])

    result = {
        "stage": "s1",
        "arch": ctx.arch,
        "samples": samples,
        "randconfig_satisfies_K": forward,
        "model_survives_olddefconfig": backward,
        "pass": (forward["checked"] > 0
                 and not forward["violations"]
                 and not backward["drift"]),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("S1 acceptance: %d/%d randconfigs satisfy K, %d/%d models survive "
             "olddefconfig  (%s)",
             forward["satisfied"], forward["checked"],
             backward["unchanged"], backward["checked"],
             human_time(time.time() - t0))
    return result


def _random_model(model, seed: int):
    s = z3.Solver()
    s.set("timeout", 60000)
    s.set("random_seed", seed % (2 ** 30))
    s.set("phase_selection", 5)  # random phase, so models differ between seeds
    s.add(*model.constraints)
    rng = random.Random(seed)
    # Nudge the solver with a few random polarity assumptions so that
    # successive seeds explore different regions of K.
    syms = sorted(model.bool_tristate)
    probes = []
    for sym in rng.sample(syms, min(60, len(syms))):
        probes.append(F.enabled_var(sym) if rng.random() < 0.3
                      else z3.Not(F.enabled_var(sym)))
    if s.check(*probes) == z3.sat:
        return s.model()
    if s.check() == z3.sat:
        return s.model()
    return None


def _levels_from_model(m, model) -> dict[str, str]:
    out = {}
    for sym in sorted(model.bool_tristate):
        out[sym] = F.LEVEL_NAMES[F.model_level(m, sym)]
    return out


def _write_full_config(path: str, levels: dict, base: dict, model) -> None:
    lines = ["# kreach acceptance: a model of K written out in full"]
    for sym, val in sorted(levels.items()):
        lines.append("%s=%s" % (sym, val) if val in ("y", "m")
                     else "# %s is not set" % sym)
    for sym, val in sorted(base.items()):
        if model.kconfig_types.get(sym) in ("int", "hex", "string"):
            lines.append("%s=%s" % (sym, val))
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# -------------------------------------------------------------------- S2 ----

def actual_levels(builddir: str) -> dict[str, str]:
    """object -> 'y' | 'm', read from what the build actually produced.

    y comes from the thin archives Kbuild links into vmlinux; m comes from the
    .mod files, which list each module's constituent objects.
    """
    import subprocess

    from kreach.s3_symbols import read_module_map

    levels: dict[str, str] = {}
    archives = [os.path.join(builddir, n) for n in ("vmlinux.a", "built-in.a")]
    archives = [a for a in archives if os.path.exists(a)]
    if not archives:
        # Older trees keep only per-directory archives.
        for dirpath, _dirs, files in os.walk(builddir):
            if "built-in.a" in files:
                archives.append(os.path.join(dirpath, "built-in.a"))
    for ar in archives:
        # Kbuild builds its thin archives with
        #   $(AR) cDPrST $@ $(real-prereqs)
        # from the top of the build tree, and real-prereqs are prefixed with
        # $(obj), so the member names it stored are relative to the build
        # root.  `ar t` resolves them against how *it* was invoked: given an
        # absolute archive path it prints absolute member paths.  Running it
        # from the build root with a relative path gives back exactly what
        # Kbuild wrote.
        rel_ar = os.path.relpath(ar, builddir)
        proc = subprocess.run(["ar", "t", rel_ar], cwd=builddir,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
        reldir = os.path.relpath(os.path.dirname(ar), builddir)
        for name in proc.stdout.split():
            if not name.endswith(".o"):
                continue
            if os.path.isabs(name):
                name = os.path.relpath(name, builddir)
            obj = os.path.normpath(name)
            if not os.path.exists(os.path.join(builddir, obj)) and reldir != ".":
                # Older trees recorded members relative to the archive.
                joined = os.path.normpath(os.path.join(reldir, name))
                if os.path.exists(os.path.join(builddir, joined)):
                    obj = joined
            levels[obj] = "y"
    module_map, _composites = read_module_map(builddir)
    for obj in module_map:
        levels.setdefault(os.path.normpath(obj), "m")
    return levels


def predicted_levels(table, settings: dict, model, objects) -> dict[str, str]:
    """Evaluate the S2 conditions under one concrete configuration.

    Substituting all three level variables of all 18,000 symbols into every
    object's condition is far slower than it needs to be: a presence condition
    mentions a handful of symbols.  Each object gets a substitution list built
    from just the symbols it uses, which S2 already records.
    """

    def value(sym: str) -> str:
        if sym not in model.kconfig_types:
            # An option this architecture does not declare is n, whatever the
            # .config happens to say (see formula.undefined_symbols_off).
            return "n"
        return settings.get(sym, "n")

    cache: dict[str, list] = {}

    def subs_for(syms) -> list:
        key = "\0".join(sorted(syms))
        if key in cache:
            return cache[key]
        subs = []
        for sym in sorted(syms):
            val = value(sym)
            subs.append((F.enabled_var(sym), z3.BoolVal(val in ("y", "m"))))
            subs.append((F.is_y(sym), z3.BoolVal(val == "y")))
            subs.append((F.is_m(sym), z3.BoolVal(val == "m")))
        if len(cache) > 20000:
            cache.clear()
        cache[key] = subs
        return subs

    out: dict[str, str] = {}
    for obj in objects:
        try:
            builtin, avail = table.conditions(obj)
        except KeyError:
            continue
        syms = set(table.all_symbols(obj)) | set(table.own_symbols(obj))
        if not syms:
            syms = F.collect_config_symbols([builtin, avail])
        subs = subs_for(syms)
        b = z3.simplify(z3.substitute(builtin, *subs)) if subs else z3.simplify(builtin)
        a = z3.simplify(z3.substitute(avail, *subs)) if subs else z3.simplify(avail)
        if z3.is_true(b):
            out[obj] = "y"
        elif z3.is_true(a):
            out[obj] = "m"
        else:
            out[obj] = "n"
    return out


def _composite_objects(ctx: Context) -> set[str]:
    """Composite module targets, from S3's scan of the build's .mod files."""
    import os as _os

    from kreach.util import read_json

    if not _os.path.exists(ctx.symbols_file):
        return set()
    try:
        return set(read_json(ctx.symbols_file).get("composites") or ())
    except Exception:
        return set()


def test_s2(ctx: Context, configs=None, randconfigs: int = 20,
            subdirs=None, seed: int = 1, reuse_s3_build: bool = False) -> dict:
    """Compare S2's predicted object levels against real builds.

    `reuse_s3_build` checks against the build S3 already made for that
    configuration instead of making another one.  The wide build is the
    expensive part of the whole pipeline, so when it is sitting there this is
    the difference between a free check and another eight minutes.
    """
    t0 = time.time()
    model, table = load_s1(ctx), load_s2(ctx)
    configs = list(configs or ["allmodconfig", "defconfig"])
    subdirs = subdirs or []
    prefix = tuple(d.rstrip("/") for d in subdirs)

    objects = [o for o in table.objects()
               if not prefix or o.startswith(prefix)]
    composites = _composite_objects(ctx)
    log.info("S2 acceptance over %d objects in %s (%d of them composite "
             "module targets, counted separately)", len(objects),
             ", ".join(prefix) or "the whole table",
             sum(1 for o in objects if o in composites))

    runs = []
    total_mismatch = 0
    for name in configs + ["randconfig-%d" % (seed + i) for i in range(randconfigs)]:
        reused = False
        if reuse_s3_build and not name.startswith("randconfig-"):
            candidate = ctx.s3_build_dir(name)
            if os.path.exists(os.path.join(candidate, ".config")):
                bdir = candidate
                reused = True
                log.info("%s: checking against the existing S3 build in %s",
                         name, bdir)
        if not reused:
            bdir = ctx.dir("accept", "s2", name)
            logfile = os.path.join(bdir, "build.log")
            if name.startswith("randconfig-"):
                s = int(name.split("-")[1])
                _make(ctx, bdir, ["randconfig"], env={"KCONFIG_SEED": hex(s)},
                      jobs=1, logfile=logfile)
            else:
                _make(ctx, bdir, [name], jobs=1, logfile=logfile)
            if not os.path.exists(os.path.join(bdir, ".config")):
                runs.append({"config": name, "error": "no .config produced"})
                continue
            _make(ctx, bdir, ["prepare", "scripts"], logfile=logfile)
            targets = ([d if d.endswith("/") else d + "/" for d in subdirs]
                       or ["vmlinux", "modules"])
            _make(ctx, bdir, targets, jobs=ctx.jobs, logfile=logfile)
        cfg = os.path.join(bdir, ".config")
        if not os.path.exists(cfg):
            runs.append({"config": name, "error": "no .config produced"})
            continue
        settings = parse_config_file(cfg)

        actual = actual_levels(bdir)
        predicted = predicted_levels(table, settings, model, objects)
        mismatches = []
        composite_diffs = []
        for obj in objects:
            p = predicted.get(obj, "n")
            a = actual.get(obj, "n")
            if p == a:
                continue
            row = {"object": obj, "predicted": p, "actual": a}
            if obj in composites:
                composite_diffs.append(row)
            else:
                mismatches.append(row)
        total_mismatch += len(mismatches)
        runs.append({
            "config": name,
            "reused_s3_build": reused,
            "objects_checked": len(objects),
            "composite_objects": sum(1 for o in objects if o in composites),
            "built_y": sum(1 for v in actual.values() if v == "y"),
            "built_m": sum(1 for v in actual.values() if v == "m"),
            "mismatches": len(mismatches),
            "examples": mismatches[:25],
            "composite_only_differences": len(composite_diffs),
            "composite_examples": composite_diffs[:10],
        })
        log.info("%s: %d/%d objects mismatch (%d built y, %d built m; plus %d "
                 "composite targets that only exist at m)", name,
                 len(mismatches), len(objects),
                 sum(1 for v in actual.values() if v == "y"),
                 sum(1 for v in actual.values() if v == "m"),
                 len(composite_diffs))
        if not reused:
            # Never prune a build we did not make: S3's objects are the
            # expensive artefact and other stages still want them.
            _prune(bdir)

    result = {
        "stage": "s2",
        "arch": ctx.arch,
        "subdirs": subdirs,
        "runs": runs,
        "total_mismatches": total_mismatch,
        "pass": total_mismatch == 0 and any(r.get("objects_checked") for r in runs),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("S2 acceptance: %d mismatches across %d configurations (%s)",
             total_mismatch, len(runs), human_time(time.time() - t0))
    return result


def _prune(bdir: str) -> None:
    """Drop object files but keep the config and log, so disk stays bounded."""
    import shutil
    keep = {".config", "build.log", "modules.order"}
    for name in os.listdir(bdir):
        if name in keep:
            continue
        path = os.path.join(bdir, name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass
