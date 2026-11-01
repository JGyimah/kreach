"""S1 -- Kconfig model.

Translate the Kconfig tree for one architecture into the formula K.

  Input:  kernel source tree, ARCH.
  Output: SMT-LIB and DIMACS formulas, plus a map from CONFIG_* names to
          variables (s1/model.smt2, s1/model.dimacs, s1/vars.json).

Reuse: kextract + kclause from the kmax suite do the Kconfig parsing and the
dependency/select/choice encoding.  kreach adds what kclause leaves out for
our purpose: level variables for *every* tristate symbol (kclause only emits
them for symbols Kconfig itself compares against y/m), the MODULES=n linkage,
and the architecture's own constraints.
"""

import os
import time

import z3

from kreach import formula as F
from kreach.config import Context
from kreach.util import get_logger, human_time, read_json, write_json

log = get_logger("s1")

# Non-visible symbols cannot be changed by the user, so their Kconfig default
# really is a constraint.  Without this, a model of K sets them arbitrarily and
# `make olddefconfig` rewrites them, which the S1 acceptance test reports as
# drift.  Visible symbols' defaults are deliberately left out: a default the
# user can change is not a constraint.
KCLAUSE_ARGS = ["--include-nonvisible-bool-defaults"]


class KconfigModel:
    """The formula K plus the symbol-type information the stages need."""

    def __init__(self, constraints: list, kconfig_types: dict, visible: set, arch: str):
        self.constraints = constraints
        self.kconfig_types = kconfig_types
        self.visible = set(visible or ())
        self.arch = arch

    @property
    def bool_tristate(self) -> set[str]:
        return {s for s, t in self.kconfig_types.items() if t in ("bool", "tristate")}

    @property
    def tristate(self) -> set[str]:
        return {s for s, t in self.kconfig_types.items() if t == "tristate"}

    def solver(self) -> z3.Solver:
        s = z3.Solver()
        s.add(*self.constraints)
        return s

    def is_tristate(self, sym: str) -> bool:
        return self.kconfig_types.get(sym) == "tristate"

    def known(self, sym: str) -> bool:
        return sym in self.kconfig_types


def _make_arch(ctx: Context, verify: bool = True):
    """An Arch for this context, with the formula files managed by us.

    kmax's own ``arch_dir`` handling is not used: in kmax 4.10 ``dump_arch``
    passes the formula *content* where its dump methods expect a *path*, so it
    cannot write the files.  Loading and dumping by explicit path avoids that
    and keeps the file names the spec's layout asks for.
    """
    from kmax.arch import Arch

    return Arch(ctx.arch, linux_ksrc=ctx.ksrc, verify_linux_ksrc=verify,
                kclause_args=KCLAUSE_ARGS)


def _load_or_generate(ctx: Context, arch, force: bool = False) -> str:
    """Make kextract and kclause available; returns the kclause SMT-LIB text.

    The kclause file is written as plain SMT-LIB rather than through
    ``Arch.dump_kclause(dump_composite=True)``, which in kmax 4.10 pickles the
    string while ``load_kclause(is_composite=True)`` reads it back as text.
    Plain text is what the loader wants and it is readable on its own.
    """
    generated = False
    if os.path.exists(ctx.kextract_file) and not force:
        log.info("loading kextract from %s", ctx.kextract_file)
        arch.load_kextract(ctx.kextract_file)
    else:
        log.info("generating kextract for %s (this parses the whole Kconfig tree)",
                 ctx.arch)
        arch.generate_kextract()
        arch.dump_kextract(ctx.kextract_file)
        log.info("wrote %s", ctx.kextract_file)
        generated = True

    if os.path.exists(ctx.kclause_file) and not force and not generated:
        log.info("loading kclause from %s", ctx.kclause_file)
        with open(ctx.kclause_file) as fh:
            return fh.read()

    log.info("generating kclause constraints")
    arch.generate_kclause()
    smt2 = arch.get_kclause_composite()
    with open(ctx.kclause_file, "w") as fh:
        fh.write(smt2)
    log.info("wrote %s", ctx.kclause_file)
    return smt2


def run_s1(ctx: Context, force: bool = False, repair: bool = True,
           r5: bool = False) -> KconfigModel:
    """Generate (or load) the Kconfig model for ctx.arch."""
    t0 = time.time()
    arch = _make_arch(ctx)
    kclause_smt2 = _load_or_generate(ctx, arch, force=force)
    log.info("kextract+kclause ready in %s", human_time(time.time() - t0))

    kconfig_types = dict(arch.get_kconfig_types() or {})
    visible = set(arch.get_kconfig_visible() or ())

    log.info("kconfig symbols: %d (%d tristate, %d bool)",
             len(kconfig_types),
             sum(1 for t in kconfig_types.values() if t == "tristate"),
             sum(1 for t in kconfig_types.values() if t == "bool"))

    constraints: list = []
    repair_report = None
    if repair:
        # Kconfig's own encoding, with the four places kclause is stricter
        # than Kconfig repaired.  See kreach/s1_repair.py.
        from kreach.s1_repair import (host_values, repair_constraints,
                                      scan_toolchain_symbols)

        with open(ctx.kextract_file) as fh:
            kextract_text = fh.read()
        toolchain = _toolchain_host_values(ctx, scan_toolchain_symbols, host_values)
        repaired, repair_report = repair_constraints(
            arch, kconfig_types, kextract_text, toolchain=toolchain,
            enable_r5=r5)
        constraints.extend(repaired)
        # Written whether or not R5 is part of K: S5 needs them to keep its
        # witnesses writable as a .config, but they are too strong to be part
        # of K itself.  See load_realisability() in kreach/s5_solve.py.
        _dump_realisability(ctx, arch, kconfig_types, kextract_text)
    else:
        constraints.extend(z3.parse_smt2_string(kclause_smt2))
    # This architecture is the one being configured.
    constraints.extend(arch.get_arch_specific_constraints())
    # Level variables for every bool/tristate symbol, and MODULES linkage.
    constraints.extend(F.completion_constraints(kconfig_types))
    constraints.extend(F.modules_off_forces_no_m(kconfig_types))

    model = KconfigModel(constraints, kconfig_types, visible, ctx.arch)

    _dump_artifacts(ctx, model)
    ctx.write_meta({"s1": {
        "symbols": len(kconfig_types),
        "tristate": len(model.tristate),
        "constraints": len(constraints),
        "repaired": bool(repair),
        "repairs": repair_report,
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S1 complete: %d constraints in %s", len(constraints), human_time(time.time() - t0))
    return model


def load_s1(ctx: Context) -> KconfigModel:
    """Load a previously generated model without touching the kernel tree."""
    if not os.path.exists(ctx.kclause_file):
        raise FileNotFoundError(
            "no S1 model in %s -- run 'kreach s1' first" % ctx.s1_dir)
    arch = _make_arch(ctx, verify=False)
    arch.load_kextract(ctx.kextract_file)
    kconfig_types = dict(arch.get_kconfig_types() or {})
    visible = set(arch.get_kconfig_visible() or ())
    # model.smt2 is the formula S1 actually built, repairs included, so later
    # stages load that rather than rebuilding it and risking a difference.
    if os.path.exists(ctx.model_file):
        constraints = list(z3.parse_smt2_string(open(ctx.model_file).read()))
    else:
        with open(ctx.kclause_file) as fh:
            constraints = list(z3.parse_smt2_string(fh.read()))
        constraints.extend(arch.get_arch_specific_constraints())
        constraints.extend(F.completion_constraints(kconfig_types))
        constraints.extend(F.modules_off_forces_no_m(kconfig_types))
    return KconfigModel(constraints, kconfig_types, visible, ctx.arch)


def _dump_realisability(ctx: Context, arch, kconfig_types: dict,
                        kextract_text: str) -> str:
    """Write the 'a non-visible symbol needs a selecter' constraints."""
    from kreach.s1_repair import KextractFacts, select_upper_bounds

    path = os.path.join(ctx.s1_dir, "realisable.smt2")
    facts = KextractFacts(kextract_text)
    visible = set(arch.get_kconfig_visible() or ())
    bounds = select_upper_bounds(arch.get_selects() or {}, kconfig_types,
                                 visible, facts.has_default)
    # A non-visible symbol that nothing selects and that has no default
    # cannot be turned on from a .config at all.
    selected = set(arch.get_selects() or {})
    unsettable = [
        z3.Not(F.enabled_var(sym))
        for sym, typ in sorted(kconfig_types.items())
        if typ in ("bool", "tristate")
        and sym not in visible
        and sym not in selected
        and sym not in facts.has_default
    ]
    F.dump_smt2(bounds + unsettable, path)
    log.info("realisability: %d select upper bounds + %d unsettable symbols "
             "-> %s", len(bounds), len(unsettable), path)
    return path


def _toolchain_host_values(ctx: Context, scan_fn, values_fn) -> dict[str, str]:
    """Ask this host's own Kconfig about the toolchain-dependent symbols.

    kextract evaluates ``$(success,...)`` and friends itself and gets them
    wrong; the spec wants them fixed to the build host's values, so we take
    those from real ``make`` runs.  The result is cached beside the model.
    """
    cache = os.path.join(ctx.s1_dir, "toolchain.json")
    if os.path.exists(cache):
        blob = read_json(cache)
        log.info("toolchain symbols: reusing %d host values from %s",
                 len(blob.get("values", {})), cache)
        return blob.get("values", {})

    symbols = scan_fn(ctx.ksrc)
    log.info("toolchain symbols: %d symbols defined with a make function",
             len(symbols))
    configs = {}
    from kreach.s3_symbols import make

    scratch = ctx.dir("s1", "hostprobe")
    for name in ("allnoconfig", "allyesconfig"):
        try:
            make(ctx, scratch, [name], jobs=1, check=False)
            path = os.path.join(scratch, ".config")
            configs[name] = parse_config_file(path) if os.path.exists(path) else {}
        except Exception as exc:
            log.warning("could not run make %s for the host probe: %s", name, exc)
            configs[name] = {}
    values = values_fn(configs.get("allnoconfig", {}),
                       configs.get("allyesconfig", {}), symbols)
    counts: dict[str, int] = {}
    for v in values.values():
        counts[v] = counts.get(v, 0) + 1
    log.info("toolchain symbols on this host: %s", counts)
    write_json(cache, {"values": values, "counts": counts})
    return values


def _dump_artifacts(ctx: Context, model: KconfigModel) -> None:
    F.dump_smt2(model.constraints, ctx.model_file)
    log.info("wrote %s", ctx.model_file)
    write_json(ctx.vars_file, {
        "arch": model.arch,
        "types": model.kconfig_types,
        "visible": sorted(model.visible),
        "convention": {
            "enabled": "CONFIG_FOO",
            "builtin": "CONFIG_FOO==y",
            "module": "CONFIG_FOO==m",
        },
    })
    log.info("wrote %s", ctx.vars_file)


def dump_dimacs(ctx: Context, model: KconfigModel) -> str:
    """Write the DIMACS form on request; it is slow for a whole arch."""
    path = os.path.join(ctx.s1_dir, "model.dimacs")
    t0 = time.time()
    var_map = F.dump_dimacs(model.constraints, path)
    write_json(os.path.join(ctx.s1_dir, "model.dimacs.vars.json"), var_map)
    log.info("wrote %s (%d vars) in %s", path, len(var_map), human_time(time.time() - t0))
    return path


# ---------------------------------------------------------------------------
# Acceptance test (spec S1): randconfig round-trip, both directions.
# ---------------------------------------------------------------------------

def parse_config_file(path: str) -> dict[str, str]:
    """Read a .config into {CONFIG_FOO: 'y'|'m'|value}.  Absent means n."""
    settings: dict[str, str] = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("# CONFIG_") and line.endswith(" is not set"):
                settings[line[2:].split(" ")[0]] = "n"
                continue
            if line.startswith("#") or "=" not in line:
                continue
            name, _, val = line.partition("=")
            name, val = name.strip(), val.strip()
            if name.startswith("CONFIG_"):
                settings[name] = val
    return settings


def config_to_assignment(settings: dict[str, str], model: KconfigModel,
                         symbols=None) -> list:
    """Turn a concrete .config into the level literals it fixes.

    `symbols` restricts the result to those names, for callers that only care
    about part of the configuration -- fixing all 18,000 symbols is wasted
    work when the formula being checked mentions a handful.
    """
    wanted = None if symbols is None else set(symbols)
    lits = []
    for sym, typ in model.kconfig_types.items():
        if typ not in ("bool", "tristate"):
            continue
        if wanted is not None and sym not in wanted:
            continue
        val = settings.get(sym, "n")
        if val == "y":
            lits.append(F.is_y(sym))
            lits.append(F.enabled_var(sym))
            lits.append(z3.Not(F.is_m(sym)))
        elif val == "m":
            lits.append(F.is_m(sym))
            lits.append(F.enabled_var(sym))
            lits.append(z3.Not(F.is_y(sym)))
        else:
            lits.append(z3.Not(F.enabled_var(sym)))
            lits.append(z3.Not(F.is_y(sym)))
            lits.append(z3.Not(F.is_m(sym)))
    return lits
