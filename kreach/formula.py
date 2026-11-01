# SPDX-License-Identifier: GPL-2.0-or-later
"""Tristate formula algebra.

kreach's whole contribution rests on telling `y` apart from `m`, so the
encoding is spelled out here rather than spread over the stages.

Variable convention (kclause's, which we extend rather than replace):

    CONFIG_FOO        FOO is enabled at *some* level, i.e. y or m
    CONFIG_FOO==y     FOO is built into vmlinux
    CONFIG_FOO==m     FOO is a module

kclause only emits the ``==y`` / ``==m`` variables for symbols that Kconfig
itself compares against y or m.  kreach needs them for every tristate symbol
that can appear as a caller or a provider, so `completion_constraints()` adds
the missing linkage:

    FOO            <->  (FOO==y) or (FOO==m)        tristate FOO
    not (FOO==y and FOO==m)                        never both true
    (FOO==m)       ->   CONFIG_MODULES              m needs module support
    FOO            <->  (FOO==y),  not (FOO==m)     bool FOO

Levels are ordered n < m < y.
"""

import re

import z3

LEVEL_N, LEVEL_M, LEVEL_Y = 0, 1, 2
LEVEL_NAMES = {LEVEL_N: "n", LEVEL_M: "m", LEVEL_Y: "y"}

MODULES = "CONFIG_MODULES"
CONFIG_RE = re.compile(r"^CONFIG_[A-Za-z0-9_]+$")
TRISTATE_VAR_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)==(y|m)$")


def enabled_var(sym: str) -> z3.BoolRef:
    """FOO enabled at any level (y or m)."""
    return z3.Bool(sym)


def level_var(sym: str, level: str) -> z3.BoolRef:
    """FOO at exactly level `y` or `m`."""
    assert level in ("y", "m"), level
    return z3.Bool("%s==%s" % (sym, level))


def is_y(sym: str) -> z3.BoolRef:
    return level_var(sym, "y")


def is_m(sym: str) -> z3.BoolRef:
    return level_var(sym, "m")


def is_n(sym: str) -> z3.BoolRef:
    return z3.Not(enabled_var(sym))


def split_tristate_var(name: str):
    """'CONFIG_FOO==m' -> ('CONFIG_FOO', 'm');  'CONFIG_FOO' -> (name, None)."""
    m = TRISTATE_VAR_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return name, None


def collect_config_symbols(exprs) -> set[str]:
    """Base CONFIG_* symbol names mentioned anywhere in `exprs`."""
    found: set[str] = set()
    stack = list(exprs)
    seen_ids: set[int] = set()
    while stack:
        e = stack.pop()
        if e is None:
            continue
        eid = e.get_id() if hasattr(e, "get_id") else id(e)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        if z3.is_const(e):
            base, _ = split_tristate_var(str(e))
            if base.startswith("CONFIG_"):
                found.add(base)
            continue
        for child in e.children():
            stack.append(child)
    return found


def completion_constraints(
    kconfig_types: dict[str, str],
    symbols: set[str] | None = None,
    include_modules_link: bool = True,
) -> list[z3.BoolRef]:
    """Constraints tying ``FOO`` to ``FOO==y`` / ``FOO==m``.

    `kconfig_types` maps CONFIG_* name -> kconfig type ("bool", "tristate",
    "int", "hex", "string").  `symbols` restricts the completion to the
    symbols we actually reason about; None means all bool/tristate symbols.
    """
    out: list[z3.BoolRef] = []
    names = set(kconfig_types) if symbols is None else set(symbols)
    for sym in sorted(names):
        typ = kconfig_types.get(sym)
        if typ not in ("bool", "tristate"):
            # Non-boolean (int/hex/string) or unknown: no level variables.
            continue
        on = enabled_var(sym)
        vy, vm = is_y(sym), is_m(sym)
        if typ == "tristate":
            # on iff y or m, and never both true
            out.append(on == z3.Or(vy, vm))
            out.append(z3.Not(z3.And(vy, vm)))
            if include_modules_link and sym != MODULES:
                out.append(z3.Implies(vm, enabled_var(MODULES)))
        else:
            out.append(on == vy)
            out.append(z3.Not(vm))
    if include_modules_link and MODULES in kconfig_types:
        # MODULES itself is a bool; keep its level variable consistent.
        out.append(enabled_var(MODULES) == is_y(MODULES))
    return out


def modules_off_forces_no_m(kconfig_types: dict[str, str]) -> list[z3.BoolRef]:
    """MODULES=n forces every ``_m`` variable false (spec S1 requirement)."""
    mod = enabled_var(MODULES)
    return [
        z3.Implies(z3.Not(mod), z3.Not(is_m(sym)))
        for sym, typ in sorted(kconfig_types.items())
        if typ == "tristate"
    ]


def undefined_symbols_off(symbols, kconfig_types: dict) -> list[z3.BoolRef]:
    """Force CONFIG symbols this architecture does not define to n.

    Kbuild refers to options that a given architecture's Kconfig never
    declares -- ``CONFIG_GENERIC_HWEIGHT`` guards ``lib/hweight.o`` but x86
    has its own ``arch/x86/lib/hweight.S`` and never selects it.  kextract
    reports no type for such a symbol, so `completion_constraints` skips it
    and its level variables are left with nothing tying them to the base
    variable: ``X==y`` can be false while ``X`` is true, which makes
    ``available(o) and not builtin(o)`` satisfiable for an object that is
    simply never built.  That reads as "the provider is a module", and is a
    whole class of false candidates.

    An option the architecture does not declare cannot be set, so it is n,
    and all three variables are false.
    """
    out: list[z3.BoolRef] = []
    for sym in sorted(set(symbols)):
        if sym in kconfig_types:
            continue
        if not CONFIG_RE.match(sym):
            continue
        out.append(z3.Not(enabled_var(sym)))
        out.append(z3.Not(is_y(sym)))
        out.append(z3.Not(is_m(sym)))
    return out


def relabel_builtin(expr, skip: set[str] | None = None):
    """Rewrite an 'is compiled' formula into an 'is built into vmlinux' one.

    A Kbuild presence condition from kmax is a formula over *enabled*
    variables, because ``obj-$(CONFIG_X) += foo.o`` puts foo.o somewhere
    whenever X is y or m.  The object lands in ``obj-y`` only when the
    selector X is y, so every selector variable becomes ``X==y``.

    `skip` holds symbols that must keep their enabled-level reading: these are
    the ones used in ``ifdef``/``ifeq`` guards, where Kbuild's test is true for
    both y and m.  S2 collects that set from the Makefiles.
    """
    skip = skip or set()
    subs = []
    for sym in sorted(collect_config_symbols([expr])):
        if sym in skip:
            continue
        subs.append((z3.Bool(sym), is_y(sym)))
    if not subs:
        return expr
    return z3.substitute(expr, *subs)


def conj(exprs) -> z3.BoolRef:
    exprs = [e for e in exprs if e is not None]
    if not exprs:
        return z3.BoolVal(True)
    if len(exprs) == 1:
        return exprs[0]
    return z3.And(*exprs)


def disj(exprs) -> z3.BoolRef:
    exprs = [e for e in exprs if e is not None]
    if not exprs:
        return z3.BoolVal(False)
    if len(exprs) == 1:
        return exprs[0]
    return z3.Or(*exprs)


def case_formula(case: str, c_builtin, c_avail, p_builtin, p_avail) -> z3.BoolRef:
    """Formula for one failure case of the core property."""
    if case == "y/m":
        return z3.And(c_builtin, z3.Not(p_builtin), p_avail)
    if case == "y/n":
        return z3.And(c_builtin, z3.Not(p_avail))
    if case == "m/n":
        return z3.And(c_avail, z3.Not(c_builtin), z3.Not(p_avail))
    raise ValueError("unknown case %r" % case)


CASES = ("y/m", "y/n", "m/n")
CASE_PRIORITY = {"y/m": "main", "y/n": "secondary", "m/n": "secondary"}


def model_level(model: z3.ModelRef, sym: str) -> int:
    """Read the level of a symbol out of a z3 model."""

    def truth(v):
        val = model.eval(v, model_completion=True)
        return z3.is_true(val)

    if truth(is_y(sym)):
        return LEVEL_Y
    if truth(is_m(sym)):
        return LEVEL_M
    return LEVEL_N


def dump_smt2(constraints, path: str, logic: str = "QF_UF") -> None:
    """Write a conjunction of constraints as SMT-LIB 2."""
    solver = z3.Solver()
    for c in constraints:
        solver.add(c)
    with open(path, "w") as fh:
        fh.write("; kreach S1 Kconfig model\n")
        fh.write("(set-logic %s)\n" % logic)
        fh.write(solver.to_smt2())


def dump_dimacs(constraints, path: str) -> dict[str, int]:
    """Write a CNF of the model in DIMACS, returning the variable map.

    Uses z3's tseitin-cnf tactic, so the result has helper variables; only the
    CONFIG_* names appear in the returned map.
    """
    goal = z3.Goal()
    goal.add(conj(list(constraints)))
    cnf = z3.Then(z3.Tactic("simplify"), z3.Tactic("tseitin-cnf"))(goal)
    var_ids: dict[str, int] = {}
    clauses: list[list[int]] = []

    def lit(e):
        neg = False
        if z3.is_not(e):
            neg, e = True, e.arg(0)
        name = str(e)
        idx = var_ids.setdefault(name, len(var_ids) + 1)
        return -idx if neg else idx

    for subgoal in cnf:
        for c in subgoal:
            if z3.is_or(c):
                clauses.append([lit(a) for a in c.children()])
            elif z3.is_true(c):
                continue
            elif z3.is_false(c):
                clauses.append([])
            else:
                clauses.append([lit(c)])

    with open(path, "w") as fh:
        fh.write("c kreach S1 Kconfig model\n")
        for name, idx in sorted(var_ids.items(), key=lambda kv: kv[1]):
            fh.write("c %d %s\n" % (idx, name))
        fh.write("p cnf %d %d\n" % (len(var_ids), len(clauses)))
        for cl in clauses:
            fh.write(" ".join(str(x) for x in cl) + " 0\n")
    return var_ids
