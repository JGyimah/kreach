# SPDX-License-Identifier: GPL-2.0-or-later
"""The S1 repairs change what K admits, so each one is pinned to the Kconfig
semantics it is meant to encode."""

import z3

from kreach import formula as F
from kreach.s1_repair import (KextractFacts, _condition_to_z3, _is_flat_off,
                              _mentions_only_constant_condition,
                              _strictly_weaker, host_values,
                              scan_toolchain_symbols, select_constraints,
                              select_upper_bounds)

KEXTRACT = """\
config CONFIG_FOO tristate
prompt CONFIG_FOO (CONFIG_MENU)
def_bool CONFIG_FOO 0|(CONFIG_MENU)
dep CONFIG_FOO (CONFIG_MENU)
config CONFIG_TWICE bool
prompt CONFIG_TWICE (1)
def_bool CONFIG_TWICE 1|(1)
def_bool CONFIG_TWICE 1|( not CONFIG_GUARD)
dep CONFIG_TWICE ( not CONFIG_GUARD)
config CONFIG_IMPLIED tristate
dep CONFIG_IMPLIED (CONFIG_MENU)
config CONFIG_SHELLY bool
def_bool CONFIG_SHELLY 0|(1)
select CONFIG_LIB CONFIG_FOO (1)
"""


def facts():
    return KextractFacts(KEXTRACT)


def test_kextract_facts_reads_each_record_kind():
    f = facts()
    assert f.declarations["CONFIG_FOO"] == 1
    assert "CONFIG_FOO" in f.has_prompt
    assert "CONFIG_FOO" in f.has_default
    assert "CONFIG_FOO" in f.has_dep
    assert "CONFIG_LIB" in f.select_targets


def test_imply_only_symbol_is_the_one_with_a_dep_and_nothing_else():
    f = facts()
    assert f.only_reachable_by_imply("CONFIG_IMPLIED") is True
    assert f.only_reachable_by_imply("CONFIG_FOO") is False


def test_r3_precondition_fires_on_the_extra_declaration():
    f = facts()
    # CONFIG_TWICE has two def_bool conditions: "(1)" and "(not GUARD)".
    assert f.declaration_conditions_differ_from_dep("CONFIG_TWICE") is True
    # CONFIG_FOO's prompt condition equals its dep, so nothing to repair.
    assert f.declaration_conditions_differ_from_dep("CONFIG_FOO") is False


def test_r3_declaration_condition_ors_the_declarations():
    allowed = facts().declaration_condition("CONFIG_TWICE")
    assert allowed is not None
    # One declaration is unconditional, so the OR is satisfiable with the
    # guard on -- which the recorded dep forbids.
    dep = z3.Not(z3.Bool("CONFIG_GUARD"))
    assert _strictly_weaker(allowed, dep) is True


def test_condition_to_z3_handles_the_constants_kextract_emits():
    assert z3.is_true(_condition_to_z3("(1)"))
    assert z3.is_false(_condition_to_z3("(0)"))
    assert _condition_to_z3("( not CONFIG_GUARD)") is not None


def test_r4_detects_a_folded_make_function_condition():
    f = facts()
    # kextract folded the whole condition to a literal: nothing left to keep.
    assert _mentions_only_constant_condition(f, "CONFIG_SHELLY") is True
    assert _mentions_only_constant_condition(f, "CONFIG_FOO") is False


def test_is_flat_off_only_matches_a_bare_negation():
    assert _is_flat_off(z3.Not(F.enabled_var("CONFIG_X")), "CONFIG_X") is True
    assert _is_flat_off(F.enabled_var("CONFIG_X"), "CONFIG_X") is False
    assert _is_flat_off(z3.Not(F.is_m("CONFIG_X")), "CONFIG_X") is False


def test_scan_toolchain_symbols_finds_make_function_definitions(tmp_path):
    (tmp_path / "Kconfig").write_text(
        "config TOOLS_SUPPORT_RELR\n"
        "\tdef_bool $(success,$(srctree)/scripts/tools-support-relr.sh)\n"
        "\n"
        "config PLAIN_BOOL\n"
        "\tbool \"plain\"\n"
        "\tdefault y\n"
        "\n"
        "config NEEDS_CC_OPTION\n"
        "\tbool\n"
        "\tdepends on $(cc-option,-fsome-flag)\n")
    found = scan_toolchain_symbols(str(tmp_path))
    assert "CONFIG_TOOLS_SUPPORT_RELR" in found
    assert "CONFIG_NEEDS_CC_OPTION" in found
    assert "CONFIG_PLAIN_BOOL" not in found


def test_host_values_classifies_from_the_two_reference_configs():
    syms = {"CONFIG_A", "CONFIG_B", "CONFIG_C"}
    got = host_values({"CONFIG_A": "y"},
                      {"CONFIG_A": "y", "CONFIG_B": "m"}, syms)
    assert got == {"CONFIG_A": "forced_on", "CONFIG_B": "available",
                   "CONFIG_C": "unavailable"}


# --- R1: the level-aware select semantics -------------------------------

TYPES = {
    "CONFIG_MODULES": "bool",
    "CONFIG_SELECTER": "tristate",
    "CONFIG_TARGET": "tristate",
    "CONFIG_BOOLSEL": "bool",
    "CONFIG_DEP": "tristate",
}


def base():
    cs = F.completion_constraints(TYPES) + F.modules_off_forces_no_m(TYPES)
    cs.append(F.enabled_var("CONFIG_MODULES"))
    return cs


def smt2(expr):
    s = z3.Solver()
    s.add(expr)
    return s.to_smt2()


def solve(cs):
    s = z3.Solver()
    s.add(*cs)
    return s


def test_tristate_selecter_at_y_forces_the_target_to_y():
    sel = {"CONFIG_TARGET": {"CONFIG_SELECTER": [smt2(z3.BoolVal(True))]}}
    cs, pairs = select_constraints(sel, TYPES)
    assert pairs == 1
    assert solve(base() + cs + [F.is_y("CONFIG_SELECTER"),
                                F.is_m("CONFIG_TARGET")]).check() == z3.unsat
    assert solve(base() + cs + [F.is_y("CONFIG_SELECTER"),
                                F.is_y("CONFIG_TARGET")]).check() == z3.sat


def test_tristate_selecter_at_m_leaves_the_target_free_to_be_a_module():
    """This is the configuration kreach exists to find, so K must admit it."""
    sel = {"CONFIG_TARGET": {"CONFIG_SELECTER": [smt2(z3.BoolVal(True))]}}
    cs, _ = select_constraints(sel, TYPES)
    assert solve(base() + cs + [F.is_m("CONFIG_SELECTER"),
                                F.is_m("CONFIG_TARGET")]).check() == z3.sat
    # ... but it must still be on.
    assert solve(base() + cs + [F.is_m("CONFIG_SELECTER"),
                                z3.Not(F.enabled_var("CONFIG_TARGET"))
                                ]).check() == z3.unsat


def test_bool_selecter_under_a_module_dependency_only_forces_m():
    """config X: bool, depends on DEP (tristate), select TARGET.

    X's own value is y, but with DEP=m the select is only active at m, so
    TARGET may be a module.  This is the VIDEO_ADV7604_CEC / CEC_CORE case.
    """
    vis = smt2(F.enabled_var("CONFIG_DEP"))
    sel = {"CONFIG_TARGET": {"CONFIG_BOOLSEL": [vis]}}
    cs, _ = select_constraints(sel, TYPES)
    on = [F.enabled_var("CONFIG_BOOLSEL"), F.is_m("CONFIG_DEP")]
    assert solve(base() + cs + on + [F.is_m("CONFIG_TARGET")]).check() == z3.sat
    # With the dependency at y the select is active at y and forces y.
    on_y = [F.enabled_var("CONFIG_BOOLSEL"), F.is_y("CONFIG_DEP")]
    assert solve(base() + cs + on_y + [F.is_m("CONFIG_TARGET")]).check() == z3.unsat


def test_an_inactive_select_constrains_nothing():
    vis = smt2(F.enabled_var("CONFIG_DEP"))
    sel = {"CONFIG_TARGET": {"CONFIG_SELECTER": [vis]}}
    cs, _ = select_constraints(sel, TYPES)
    off = [z3.Not(F.enabled_var("CONFIG_DEP")), F.is_y("CONFIG_SELECTER")]
    assert solve(base() + cs + off + [z3.Not(F.enabled_var("CONFIG_TARGET"))
                                      ]).check() == z3.sat


def test_r5_bounds_a_nonvisible_target_above():
    sel = {"CONFIG_TARGET": {"CONFIG_SELECTER": [smt2(z3.BoolVal(True))]}}
    cs, _ = select_constraints(sel, TYPES)
    upper = select_upper_bounds(sel, TYPES, visible=set(), has_default=set())
    assert upper
    # Nothing selects it at y, so it may not be y.
    assert solve(base() + cs + upper + [F.is_m("CONFIG_SELECTER"),
                                        F.is_y("CONFIG_TARGET")]).check() == z3.unsat
    # A visible target keeps its freedom.
    upper_vis = select_upper_bounds(sel, TYPES, visible={"CONFIG_TARGET"},
                                    has_default=set())
    assert upper_vis == []


# --- R6: depends on caps a tristate's level -----------------------------

def test_r6_tristate_cannot_be_y_when_its_dependency_is_a_module():
    """config SND_SOC / tristate / depends on SND.

    Kconfig caps a symbol at the value of its dependencies, so SND=m means
    SND_SOC can be at most m.  kclause's Boolean implication misses this and
    S5 then writes witnesses Kconfig silently downgrades.
    """
    from kreach.s1_repair import dependency_level_clamps

    types = dict(TYPES)
    f = KextractFacts("dep CONFIG_TARGET (CONFIG_DEP)\n")
    clamps = dependency_level_clamps(f, types)
    assert len(clamps) == 1
    base_cs = base() + [z3.Implies(F.enabled_var("CONFIG_TARGET"),
                                   F.enabled_var("CONFIG_DEP"))]
    # Without the clamp, y-over-m is admitted.
    assert solve(base_cs + [F.is_y("CONFIG_TARGET"),
                            F.is_m("CONFIG_DEP")]).check() == z3.sat
    # With it, it is not.
    assert solve(base_cs + clamps + [F.is_y("CONFIG_TARGET"),
                                     F.is_m("CONFIG_DEP")]).check() == z3.unsat
    # m over m stays fine, and y over y stays fine.
    assert solve(base_cs + clamps + [F.is_m("CONFIG_TARGET"),
                                     F.is_m("CONFIG_DEP")]).check() == z3.sat
    assert solve(base_cs + clamps + [F.is_y("CONFIG_TARGET"),
                                     F.is_y("CONFIG_DEP")]).check() == z3.sat


def test_r6_leaves_bool_targets_alone():
    """Kconfig promotes a bool whose visibility is 'mod' back to y, so a bool
    depending on a module may still be built in."""
    from kreach.s1_repair import dependency_level_clamps

    types = dict(TYPES)
    types["CONFIG_BOOLTGT"] = "bool"
    f = KextractFacts("dep CONFIG_BOOLTGT (CONFIG_DEP)\n")
    assert dependency_level_clamps(f, types) == []


def test_r6_skips_dependencies_with_no_tristate_in_them():
    from kreach.s1_repair import dependency_level_clamps

    f = KextractFacts("dep CONFIG_TARGET (CONFIG_MODULES)\n")
    assert dependency_level_clamps(f, TYPES) == []


def test_r6_keeps_the_level_test_in_a_hand_rolled_kernel_guard():
    """drivers/scsi/Kconfig writes its own reachability guard:

        config SCSI_ENCLOSURE
                tristate
                depends on SCSI && ENCLOSURE_SERVICES
                depends on m || SCSI_SAS_ATTRS != m

    The "!= m" has to survive into the clamp.  kclause's dir_dep collapses it
    to "SCSI_SAS_ATTRS must be off", which rejects configurations Kconfig
    accepts -- measured on three of eight randconfigs.
    """
    from kreach.s1_repair import dependency_level_clamps

    types = {"CONFIG_MODULES": "bool", "CONFIG_SCSI": "tristate",
             "CONFIG_ENCLOSURE_SERVICES": "tristate",
             "CONFIG_SCSI_ENCLOSURE": "tristate",
             "CONFIG_SCSI_SAS_ATTRS": "tristate"}
    f = KextractFacts(
        "dep CONFIG_SCSI_ENCLOSURE (CONFIG_SCSI and CONFIG_ENCLOSURE_SERVICES "
        "and (1 and CONFIG_MODULES or  not CONFIG_SCSI_SAS_ATTRS==m))\n")
    clamps = dependency_level_clamps(f, types)
    assert len(clamps) == 1

    cs = F.completion_constraints(types) + F.modules_off_forces_no_m(types) + clamps
    on = [F.is_y("CONFIG_SCSI_ENCLOSURE"), F.is_y("CONFIG_SCSI_SAS_ATTRS"),
          F.is_y("CONFIG_SCSI"), F.is_y("CONFIG_ENCLOSURE_SERVICES")]
    # MODULES=n: nothing can be m, so the guard is satisfied and both may be y.
    s = z3.Solver()
    s.add(*cs, *on, z3.Not(F.enabled_var("CONFIG_MODULES")))
    assert s.check() == z3.sat
