"""S1 repairs: two places where kclause's K is stricter than Kconfig.

The S1 acceptance test (``kreach test s1``) compares K against real
``make randconfig`` output.  Two classes of disagreement show up, and both
would cost kreach findings, so S1 repairs them and reports how often.

R1 -- a tristate that is ``select``ed may still be a module.
    kclause emits, for every tristate X with reverse dependencies R,

        X==m  ->  not R

    which reads "if X is a module, nothing selects it".  That is too strong:
    Kconfig's ``select`` raises X to the *value of the selecting symbol*, so a
    tristate selected only by modules is a module.  The constraint forbids
    exactly the configurations kreach exists to find -- a provider at m -- so
    it is replaced by the level-aware version, built from kclause's own
    per-selecter data:

        active_y(S)   ->  X==y        a select active at y forces X to y
        active_on(S)  ->  X enabled   a select active at m forces X on

    where the select's strength is the *tristate* minimum of the selecting
    symbol's value and its visibility condition.  That second part matters:

        config VIDEO_ADV7604_CEC
                bool "..."
                depends on VIDEO_ADV7604     # tristate
                select CEC_CORE              # tristate

    is a bool, so its own value is y, but with VIDEO_ADV7604=m the select is
    only active at m and CEC_CORE may be a module.  ``active_y`` therefore
    requires every tristate in the visibility condition to be y as well.

R5 -- a non-visible selected symbol takes exactly its selecters' value.
    R1 gives the lower bound.  For a symbol the user cannot set and that has
    no default, the value is also bounded *above* by what selects it, which is
    why ``make olddefconfig`` rewrites a model of K that sets such a symbol to
    y when only a module selects it.  **Off by default.**  Measured on v6.17
    x86_64 it rejects configurations that ``make randconfig`` really produces,
    because kclause's per-selecter data does not capture every route to a
    symbol being set.  Unlike R1-R4 this repair *adds* constraints, and the
    costs are not symmetric: an unsound added constraint loses findings
    silently, while an unsound relaxation costs at most one witness that S5
    rejects before any build.  Enable it with ``kreach s1 --r5`` to experiment.

R2 -- ``imply`` carries no constraint, but kclause forces the symbol off.
    kextract does not record ``imply`` at all.  A symbol whose only route to
    being enabled is an ``imply`` therefore looks orphaned to kclause, which
    forces it false outright.  The spec asks for ``imply`` to impose no
    constraint, so the flat ``not X`` is replaced by X's own dependency,
    ``X -> dep(X)``.

R3 -- a symbol declared in more than one Kconfig file keeps only one of its
    dependencies.  ``CPU_MITIGATIONS`` is declared both in ``arch/Kconfig``
    under ``if !ARCH_CONFIGURES_CPU_MITIGATIONS`` and, for x86, as a plain
    ``menuconfig`` with no dependency.  kextract records the guarded
    dependency and an unconditional prompt, and kclause keeps only the
    dependency -- so on x86, which selects ARCH_CONFIGURES_CPU_MITIGATIONS,
    the symbol and everything under it becomes unreachable.  The repair ORs
    the declaration conditions back in.

R4 -- ``$(shell ...)`` and toolchain tests are mis-evaluated.
    kextract evaluates ``$(success,...)``, ``$(cc-option,...)`` and friends
    itself, outside the kernel's own make, and gets 0 where the real
    toolchain gets 1: ``TOOLS_SUPPORT_RELR`` comes out ``def_bool 0`` and
    ``X86_X32_ABI`` comes out with dependency ``0``.  The spec asks for these
    to be fixed to the values of the build host, so they are taken from real
    ``make allnoconfig``/``allyesconfig`` runs on this machine.

R6 -- ``depends on`` loses the level clamp.
    kclause encodes ``depends on`` as a Boolean implication, so K admits
    ``SND_SOC==y`` while ``SND==m``.  Kconfig does not: a tristate symbol's
    value is capped by the value of its dependency expression, so with SND=m
    the most SND_SOC can be is m.  Without this, S5 produces witnesses that
    Kconfig silently downgrades.  (Bool symbols are not capped this way --
    Kconfig promotes a bool whose visibility is ``mod`` back to y -- so the
    clamp is added for tristate targets only.)

All of these repairs *relax* or *re-pin* K, which is safe by construction in kreach: every
witness S5 produces is materialised by the kernel's own Kconfig and checked
to still have the levels the query asked for, and S6 then confirms it with a
build.  A relaxation that is wrong costs a rejected witness, never a false
finding.
"""

import re
import time

import z3

from kreach import formula as F
from kreach.util import get_logger

log = get_logger("s1-repair")

RE_CONFIG_DECL = re.compile(r"^config\s+(CONFIG_[A-Za-z0-9_]+)\s+(\S+)")
RE_PROMPT = re.compile(r"^prompt\s+(CONFIG_[A-Za-z0-9_]+)\s")
RE_DEF = re.compile(r"^def_(?:bool|nonbool)\s+(CONFIG_[A-Za-z0-9_]+)\s")
RE_SELECT = re.compile(r"^select\s+(CONFIG_[A-Za-z0-9_]+)\s")
RE_DEP = re.compile(r"^dep\s+(CONFIG_[A-Za-z0-9_]+)\s")


class KextractFacts:
    """What kextract records about each symbol, for deciding repairs."""

    def __init__(self, text: str):
        self.declarations: dict[str, int] = {}
        self.has_prompt: set[str] = set()
        self.has_default: set[str] = set()
        self.select_targets: set[str] = set()
        self.has_dep: set[str] = set()
        self.decl_conditions: dict[str, list[str]] = {}
        self.dep_conditions: dict[str, list[str]] = {}
        for line in text.splitlines():
            if not line or line[0] not in "cpdsr":
                continue
            m = RE_CONFIG_DECL.match(line)
            if m:
                self.declarations[m.group(1)] = self.declarations.get(m.group(1), 0) + 1
                continue
            for regex, sink in ((RE_PROMPT, self.has_prompt),
                                (RE_DEF, self.has_default),
                                (RE_SELECT, self.select_targets),
                                (RE_DEP, self.has_dep)):
                m = regex.match(line)
                if not m:
                    continue
                sym = m.group(1)
                sink.add(sym)
                if regex is RE_PROMPT:
                    self.decl_conditions.setdefault(sym, []).append(
                        line[m.end():].strip())
                elif regex is RE_DEF:
                    # "def_bool CONFIG_X <value>|(<condition>)"
                    rest = line[m.end():].strip()
                    _, _, cond = rest.partition("|")
                    self.decl_conditions.setdefault(sym, []).append(cond.strip())
                elif regex is RE_DEP:
                    self.dep_conditions.setdefault(sym, []).append(
                        line[m.end():].strip())
                break

    def declaration_conditions_differ_from_dep(self, sym: str) -> bool:
        """Cheap precondition for R3.

        kextract writes one ``config`` line per symbol however many times it
        is declared, so the extra declarations only show up as extra
        ``prompt``/``def_bool`` lines.  Two distinct declaration conditions,
        or one that differs from the recorded dependency, is the signal worth
        checking properly.
        """
        decls = {c.strip() for c in self.decl_conditions.get(sym, ())}
        if not decls:
            return False
        if len(decls) > 1:
            return True
        deps = {c.strip() for c in self.dep_conditions.get(sym, ())}
        return bool(deps) and decls != deps

    def declaration_condition(self, sym: str):
        """OR of the conditions under which any declaration of `sym` applies."""
        parts = []
        for cond in self.decl_conditions.get(sym, ()):
            e = _condition_to_z3(cond)
            if e is None:
                return None
            parts.append(e)
        return F.disj(parts) if parts else None

    def dependency(self, sym: str):
        """The symbol's ``depends on`` condition, with level tests intact.

        kclause's own ``get_dir_dep()`` is generated with tristate support
        disabled -- it says so on stderr -- which collapses ``X==m`` to ``X``.
        That matters for exactly the constructs kreach cares about:

            config SCSI_ENCLOSURE
                    tristate
                    depends on SCSI && ENCLOSURE_SERVICES
                    depends on m || SCSI_SAS_ATTRS != m

        the kernel's own hand-rolled reachability guard.  Collapsed, it reads
        "SCSI_SAS_ATTRS must be off", and rejects configurations Kconfig
        accepts.  kextract's ``dep`` line keeps the ``==m``, so it is the one
        to use.  Several dep lines are per-declaration alternatives and are
        ORed, as in `declaration_condition`.
        """
        parts = []
        for cond in self.dep_conditions.get(sym, ()):
            e = _condition_to_z3(cond)
            if e is None:
                return None
            parts.append(e)
        return F.disj(parts) if parts else None

    def only_reachable_by_imply(self, sym: str) -> bool:
        """No prompt, no default and no select, but it does have a dependency.

        That is what a symbol enabled solely through ``imply`` looks like
        after kextract, since kextract drops imply entirely.
        """
        return (sym not in self.has_prompt
                and sym not in self.has_default
                and sym not in self.select_targets
                and sym in self.has_dep)


RE_KCONFIG_FUNC = re.compile(r"\$\((?:success|failure|cc-option|cc-option-bit|"
                             r"ld-option|as-instr|as-option|rustc-option|"
                             r"rustc-min-version|cc-min-version|ld-min-version|"
                             r"m32-flag|m64-flag|shell|shell-cached|as-instr64)")
RE_KCONFIG_CONFIG = re.compile(r"^\s*(?:menu)?config\s+([A-Z0-9_]+)\s*$")


def scan_toolchain_symbols(ksrc: str) -> set[str]:
    """Symbols whose Kconfig definition calls a make function.

    These are the ``$(shell ...)``/toolchain-dependent symbols: their value
    is a property of the build host, not of the configuration.
    """
    import os

    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(ksrc):
        dirnames[:] = [d for d in dirnames if d not in (".git", "Documentation")]
        for fn in filenames:
            if not (fn == "Kconfig" or fn.startswith("Kconfig")):
                continue
            try:
                lines = open(os.path.join(dirpath, fn), errors="replace").read().splitlines()
            except OSError:
                continue
            current = None
            for line in lines:
                m = RE_KCONFIG_CONFIG.match(line)
                if m:
                    current = "CONFIG_" + m.group(1)
                    continue
                if not line.startswith((" ", "\t")):
                    # left the stanza
                    if line.strip() and not line.lstrip().startswith("#"):
                        current = None
                    continue
                if current and RE_KCONFIG_FUNC.search(line):
                    found.add(current)
    return found


def host_values(allnoconfig: dict, allyesconfig: dict, symbols: set[str]) -> dict[str, str]:
    """What this host's own Kconfig makes of each toolchain symbol.

    allnoconfig pins what is forced on regardless of configuration;
    allyesconfig says whether the symbol is available on this host at all.
    """
    out: dict[str, str] = {}
    for sym in sorted(symbols):
        lo = allnoconfig.get(sym, "n")
        hi = allyesconfig.get(sym, "n")
        if lo in ("y", "m"):
            out[sym] = "forced_on"
        elif hi in ("y", "m"):
            out[sym] = "available"
        else:
            out[sym] = "unavailable"
    return out


def _condition_to_z3(cond: str):
    """Parse a kextract condition into z3, or None if it cannot be parsed."""
    cond = cond.strip()
    if cond.startswith("(") and cond.endswith(")"):
        cond = cond[1:-1].strip()
    if cond in ("1", "y"):
        return z3.BoolVal(True)
    if cond in ("0", "n", ""):
        return z3.BoolVal(False)
    try:
        from kmax.expression_converter import convert_to_z3

        expr = convert_to_z3(cond, allow_tristate=True)
    except Exception:
        return None
    # Conditions over non-boolean symbols (string or integer comparisons) come
    # back with a non-boolean sort; R3 has nothing to say about those.
    if expr is None or not z3.is_bool(expr):
        return None
    return expr


def select_constraints(selects: dict, kconfig_types: dict) -> tuple[list, int]:
    """Kconfig's ``select`` semantics, with levels.

    `selects` is kclause's mapping ``{selected: {selecter: [visibility
    clauses in SMT-LIB]}}``.  ``select`` raises the selected symbol to the
    value of the selecter, so a y selecter forces y while an m selecter only
    forces the symbol on.  Several ``select`` statements for the same pair are
    alternatives, so their visibility conditions are ORed.
    """
    out: list = []
    pairs = 0
    lower_bounds: dict[str, list] = {}
    for target in sorted(selects):
        ttype = kconfig_types.get(target)
        if ttype not in ("bool", "tristate"):
            continue
        entry = selects[target]
        if not isinstance(entry, dict):
            continue
        for selecter, clauses in sorted(entry.items()):
            stype = kconfig_types.get(selecter)
            if stype not in ("bool", "tristate"):
                continue
            vis_parts = []
            for clause in (clauses if isinstance(clauses, list) else [clauses]):
                if not clause:
                    vis_parts.append(z3.BoolVal(True))
                    continue
                try:
                    vis_parts.append(F.conj(z3.parse_smt2_string(clause)))
                except Exception:
                    vis_parts.append(z3.BoolVal(True))
            vis_on = F.disj(vis_parts) if vis_parts else z3.BoolVal(True)
            # The select's strength is the tristate minimum of the selecter's
            # value and its visibility, so "active at y" needs every tristate
            # in the visibility condition to be y too.
            tristates = {s for s in F.collect_config_symbols([vis_on])
                         if kconfig_types.get(s) == "tristate"}
            vis_y = F.relabel_builtin(
                vis_on, skip={s for s in F.collect_config_symbols([vis_on])
                              if s not in tristates})
            sel_on = F.enabled_var(selecter)
            sel_y = F.is_y(selecter) if stype == "tristate" else sel_on
            active_on = z3.And(vis_on, sel_on)
            active_y = z3.And(vis_y, sel_y)
            pairs += 1
            if ttype == "bool":
                # A bool target cannot be a module, so any active select
                # forces it on.
                out.append(z3.Implies(active_on, F.enabled_var(target)))
                continue
            out.append(z3.Implies(active_y, F.is_y(target)))
            out.append(z3.Implies(active_on, F.enabled_var(target)))
            lower_bounds.setdefault(target, []).append((active_y, active_on))
    return out, pairs


def select_upper_bounds(selects: dict, kconfig_types: dict, visible: set,
                        has_default: set) -> list:
    """R5: a non-visible selected symbol with no default takes exactly the
    value its selecters give it, so bound it above as well as below."""
    out: list = []
    for target in sorted(selects):
        if kconfig_types.get(target) != "tristate":
            continue
        if target in visible or target in has_default:
            continue
        entry = selects[target]
        if not isinstance(entry, dict):
            continue
        ons, ys = [], []
        for selecter, clauses in sorted(entry.items()):
            stype = kconfig_types.get(selecter)
            if stype not in ("bool", "tristate"):
                continue
            vis_parts = []
            for clause in (clauses if isinstance(clauses, list) else [clauses]):
                if not clause:
                    vis_parts.append(z3.BoolVal(True))
                    continue
                try:
                    vis_parts.append(F.conj(z3.parse_smt2_string(clause)))
                except Exception:
                    vis_parts.append(z3.BoolVal(True))
            vis_on = F.disj(vis_parts) if vis_parts else z3.BoolVal(True)
            tristates = {s for s in F.collect_config_symbols([vis_on])
                         if kconfig_types.get(s) == "tristate"}
            vis_y = F.relabel_builtin(
                vis_on, skip={s for s in F.collect_config_symbols([vis_on])
                              if s not in tristates})
            sel_on = F.enabled_var(selecter)
            sel_y = F.is_y(selecter) if stype == "tristate" else sel_on
            ons.append(z3.And(vis_on, sel_on))
            ys.append(z3.And(vis_y, sel_y))
        if not ons:
            continue
        out.append(z3.Implies(F.enabled_var(target), F.disj(ons)))
        out.append(z3.Implies(F.is_y(target), F.disj(ys)))
    return out


def dependency_level_clamps(facts: "KextractFacts", kconfig_types: dict) -> list:
    """R6: a tristate is y only if its dependency expression is y.

    ``config SND_SOC / tristate / depends on SND`` cannot be y while SND is a
    module, because Kconfig caps a symbol's value at the value of its
    dependencies.  The cap uses the same relabelling S2 uses for object
    levels: every tristate in the dependency must itself be y.

    One deliberate under-approximation: Kconfig's ``m`` keyword in a
    dependency (``depends on m || ...``) makes the expression's value m, so the
    symbol should be capped to m.  kextract renders it as ``CONFIG_MODULES``,
    which the relabelling cannot tell from a real ``depends on MODULES``, so
    the clamp is weaker than Kconfig there.  That is the safe direction: it
    admits configurations Kconfig would cap, which S5 then rejects when it
    materialises the witness, rather than removing them silently.
    """
    out: list = []
    for sym in sorted(facts.dep_conditions):
        if kconfig_types.get(sym) != "tristate":
            continue
        dep = facts.dependency(sym)
        if dep is None:
            continue
        syms = F.collect_config_symbols([dep])
        tristates = {s for s in syms if kconfig_types.get(s) == "tristate"}
        if not tristates:
            continue
        dep_y = F.relabel_builtin(dep, skip=syms - tristates)
        out.append(z3.Implies(F.is_y(sym), dep_y))
    return out


def _dependency_of(facts: "KextractFacts", sym: str, dir_dep: dict):
    """kextract's dependency for `sym`, falling back to kclause's."""
    dep = facts.dependency(sym)
    if dep is not None:
        return dep
    if sym in dir_dep:
        try:
            return F.conj(z3.parse_smt2_string(dir_dep[sym]))
        except Exception:
            return None
    return None


def _strictly_weaker(allowed, dep) -> bool:
    """Is there a configuration `allowed` admits but `dep` forbids?"""
    s = z3.Solver()
    s.set("timeout", 5000)
    s.add(z3.And(allowed, z3.Not(dep)))
    return s.check() == z3.sat


def _equivalent(a, b) -> bool:
    s = z3.Solver()
    s.set("timeout", 5000)
    s.add(a != b)
    return s.check() == z3.unsat


def repair_constraints(arch, kconfig_types: dict, kextract_text: str,
                       toolchain: dict[str, str] | None = None,
                       enable_r1: bool = True, enable_r2: bool = True,
                       enable_r3: bool = True, enable_r4: bool = True,
                       enable_r5: bool = False, enable_r6: bool = True):
    """Rebuild the kclause constraint list with R1..R6 applied.

    `toolchain` maps a toolchain-dependent symbol to what this host makes of
    it: "forced_on", "available" or "unavailable" (see `host_values`).

    Returns (constraints, report).
    """
    t0 = time.time()
    facts = KextractFacts(kextract_text)
    per_var = arch.get_kclause()
    rev_dep = arch.get_rev_dep() or {}
    dir_dep = arch.get_dir_dep() or {}
    toolchain = toolchain or {}

    constraints: list = []
    r1_dropped: list[str] = []
    r2_relaxed: list[str] = []
    r2_skipped: list[str] = []
    r3_relaxed: list[str] = []
    r4_pinned: dict[str, str] = {}
    rev_cache: dict[str, object] = {}

    for var in sorted(per_var):
        is_tristate = kconfig_types.get(var) == "tristate"
        m_var_name = "%s==m" % var

        # R4: a toolchain-dependent symbol is whatever this host makes of it,
        # so kclause's clauses for it are replaced outright.
        if enable_r4 and var in toolchain:
            verdict = toolchain[var]
            if verdict == "forced_on":
                # allnoconfig already has it on, so this host forces it.
                constraints.append(F.enabled_var(var))
                r4_pinned[var] = verdict
                continue
            if _mentions_only_constant_condition(facts, var):
                # kextract folded the symbol's whole condition into a literal
                # 0 or 1 when it evaluated the make function, so there is no
                # dependency left to keep.  Leave the symbol unconstrained
                # rather than pinning it off: allyesconfig does not enable
                # everything (choice members, mutually exclusive debug
                # options), so "not on in allyesconfig" is not evidence that
                # the host cannot have it.  Over-relaxing costs at most a
                # witness that S5 rejects; pinning it off would lose findings
                # silently.
                r4_pinned[var] = verdict
                continue

        # R3: a symbol declared more than once keeps only one dependency.
        # It fires when some declaration admits the symbol in a configuration
        # the recorded dependency forbids.
        if enable_r3 and facts.declaration_conditions_differ_from_dep(var):
            allowed = facts.declaration_condition(var)
            dep = _dependency_of(facts, var, dir_dep)
            if allowed is not None and dep is not None:
                if _strictly_weaker(allowed, dep):
                    constraints.append(
                        z3.Implies(F.enabled_var(var), z3.Or(dep, allowed)))
                    r3_relaxed.append(var)
                    continue

        for clause_str in per_var[var]:
            parsed = z3.parse_smt2_string(clause_str)
            expr = F.conj(parsed)

            # R1: drop "X==m -> not rev_dep(X)".
            if (enable_r1 and is_tristate and var in rev_dep
                    and m_var_name in clause_str):
                if var not in rev_cache:
                    rev_cache[var] = F.conj(z3.parse_smt2_string(rev_dep[var]))
                target = z3.Or(z3.Not(F.is_m(var)), z3.Not(rev_cache[var]))
                if _equivalent(expr, target):
                    r1_dropped.append(var)
                    continue

            # R2: replace a flat "not X" with X's own dependency.
            if enable_r2 and _is_flat_off(expr, var):
                if facts.only_reachable_by_imply(var):
                    dep = _dependency_of(facts, var, dir_dep)
                    if dep is not None:
                        constraints.append(z3.Implies(F.enabled_var(var), dep))
                    r2_relaxed.append(var)
                    continue
                r2_skipped.append(var)

            constraints.append(expr)

    clamps = dependency_level_clamps(facts, kconfig_types) if enable_r6 else []
    constraints.extend(clamps)

    sel, pairs, upper = [], 0, []
    if enable_r1:
        selects = arch.get_selects() or {}
        sel, pairs = select_constraints(selects, kconfig_types)
        constraints.extend(sel)
        if enable_r5:
            visible = set(arch.get_kconfig_visible() or ())
            upper = select_upper_bounds(selects, kconfig_types, visible,
                                        facts.has_default)
            constraints.extend(upper)

    report = {
        "r1_select_at_m_dropped": len(r1_dropped),
        "r1_level_aware_select_constraints": len(sel),
        "r1_select_pairs": pairs,
        "r5_nonvisible_upper_bounds": len(upper),
        "r6_dependency_level_clamps": len(clamps),
        "r1_symbols": sorted(set(r1_dropped))[:50],
        "r2_imply_only_relaxed": len(set(r2_relaxed)),
        "r2_symbols": sorted(set(r2_relaxed))[:50],
        "r2_forced_off_left_alone": len(set(r2_skipped)),
        "r3_multiply_declared_relaxed": len(set(r3_relaxed)),
        "r3_symbols": sorted(set(r3_relaxed))[:50],
        "r4_toolchain_pinned": len(r4_pinned),
        "r4_by_verdict": {v: sum(1 for x in r4_pinned.values() if x == v)
                          for v in sorted(set(r4_pinned.values()))},
        "clauses_kept": len(constraints),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("S1 repairs: R1 replaced %d select-at-m constraints with %d "
             "level-aware ones over %d select pairs; R2 relaxed %d imply-only; "
             "R3 relaxed %d multiply-declared; R4 handled %d toolchain symbols "
             "(%s); R5 added %d non-visible upper bounds; R6 added %d "
             "dependency level clamps; %d forced-off symbols left alone (%.1fs)",
             report["r1_select_at_m_dropped"],
             report["r1_level_aware_select_constraints"],
             report["r1_select_pairs"], report["r2_imply_only_relaxed"],
             report["r3_multiply_declared_relaxed"], report["r4_toolchain_pinned"],
             report["r4_by_verdict"], report["r5_nonvisible_upper_bounds"],
             report["r6_dependency_level_clamps"],
             report["r2_forced_off_left_alone"], report["seconds"])
    return constraints, report


def _mentions_only_constant_condition(facts: KextractFacts, var: str) -> bool:
    """Is every recorded condition for this symbol a constant?

    That is the fingerprint of a mis-evaluated make function: kextract
    resolved ``$(success,...)`` to a literal 0 or 1 rather than leaving a
    dependency behind.
    """
    conds = list(facts.decl_conditions.get(var, ())) + \
        list(facts.dep_conditions.get(var, ()))
    if not conds:
        return True
    for cond in conds:
        c = cond.strip().strip("()").strip()
        if c not in ("0", "1", "y", "n", ""):
            return False
    return True


def _is_flat_off(expr, var: str) -> bool:
    """Is this clause just 'not X'?"""
    simplified = z3.simplify(expr)
    if not z3.is_not(simplified):
        return False
    inner = simplified.arg(0)
    return z3.is_const(inner) and str(inner) == var
