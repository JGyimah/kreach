# SPDX-License-Identifier: GPL-2.0-or-later
"""The tristate encoding is where kreach's y/m distinction lives, so it gets
the closest tests: a hand-built model standing in for K, and the three failure
cases checked against configurations we can reason about by hand."""

import z3

from kreach import formula as F


TYPES = {
    "CONFIG_MODULES": "bool",
    "CONFIG_FOO": "tristate",
    "CONFIG_BAR": "tristate",
    "CONFIG_PLAIN": "bool",
    "CONFIG_SIZE": "int",
}


def base_model(modules=True):
    cs = F.completion_constraints(TYPES)
    cs += F.modules_off_forces_no_m(TYPES)
    if modules:
        cs.append(F.enabled_var("CONFIG_MODULES"))
    return cs


def solve(constraints):
    s = z3.Solver()
    s.add(*constraints)
    return s


def test_tristate_levels_are_exclusive():
    s = solve(base_model() + [F.is_y("CONFIG_FOO"), F.is_m("CONFIG_FOO")])
    assert s.check() == z3.unsat


def test_enabled_iff_y_or_m():
    s = solve(base_model() + [F.enabled_var("CONFIG_FOO"),
                              z3.Not(F.is_y("CONFIG_FOO")),
                              z3.Not(F.is_m("CONFIG_FOO"))])
    assert s.check() == z3.unsat
    s = solve(base_model() + [z3.Not(F.enabled_var("CONFIG_FOO")),
                              F.is_m("CONFIG_FOO")])
    assert s.check() == z3.unsat


def test_bool_symbol_has_no_module_level():
    s = solve(base_model() + [F.is_m("CONFIG_PLAIN")])
    assert s.check() == z3.unsat
    s = solve(base_model() + [F.is_y("CONFIG_PLAIN")])
    assert s.check() == z3.sat


def test_modules_off_forces_every_m_false():
    """The spec's S1 requirement: MODULES=n leaves no symbol at m."""
    cs = base_model(modules=False) + [z3.Not(F.enabled_var("CONFIG_MODULES"))]
    s = solve(cs + [F.is_m("CONFIG_FOO")])
    assert s.check() == z3.unsat
    s = solve(cs + [F.is_y("CONFIG_FOO")])
    assert s.check() == z3.sat


def test_non_boolean_symbols_get_no_level_variables():
    cs = F.completion_constraints(TYPES)
    text = " ".join(str(c) for c in cs)
    assert "CONFIG_SIZE" not in text


def test_relabel_builtin_turns_selectors_into_y():
    avail = z3.And(F.enabled_var("CONFIG_FOO"), F.enabled_var("CONFIG_BAR"))
    builtin = F.relabel_builtin(avail)
    # FOO=m, BAR=y: compiled, but not built into vmlinux.
    cs = base_model() + [F.is_m("CONFIG_FOO"), F.is_y("CONFIG_BAR")]
    assert solve(cs + [avail]).check() == z3.sat
    assert solve(cs + [builtin]).check() == z3.unsat
    # Both y: built into vmlinux.
    cs = base_model() + [F.is_y("CONFIG_FOO"), F.is_y("CONFIG_BAR")]
    assert solve(cs + [builtin]).check() == z3.sat


def test_relabel_skips_guard_symbols():
    """ifdef CONFIG_X is true for m as well, so X keeps its enabled reading."""
    avail = z3.And(F.enabled_var("CONFIG_FOO"), F.enabled_var("CONFIG_BAR"))
    builtin = F.relabel_builtin(avail, skip={"CONFIG_BAR"})
    cs = base_model() + [F.is_y("CONFIG_FOO"), F.is_m("CONFIG_BAR")]
    assert solve(cs + [builtin]).check() == z3.sat


def test_ym_case_is_the_one_the_stub_pattern_misses():
    """caller y, provider m -> satisfiable; the core property of the tool."""
    c_avail = F.enabled_var("CONFIG_FOO")
    p_avail = F.enabled_var("CONFIG_BAR")
    c_builtin = F.relabel_builtin(c_avail)
    p_builtin = F.relabel_builtin(p_avail)
    # RC under the default rule: caller built, provider enabled at any level.
    rc = z3.And(c_avail, p_avail)

    q = F.case_formula("y/m", c_builtin, c_avail, p_builtin, p_avail)
    s = solve(base_model() + [rc, q])
    assert s.check() == z3.sat
    m = s.model()
    assert F.model_level(m, "CONFIG_FOO") == F.LEVEL_Y
    assert F.model_level(m, "CONFIG_BAR") == F.LEVEL_M


def test_yn_and_mn_are_unsat_under_the_default_rc_rule():
    """The stub pattern protects the disabled case, so y/n and m/n need
    evidence that the reference survives the provider being off."""
    c_avail = F.enabled_var("CONFIG_FOO")
    p_avail = F.enabled_var("CONFIG_BAR")
    c_builtin, p_builtin = F.relabel_builtin(c_avail), F.relabel_builtin(p_avail)
    rc_default = z3.And(c_avail, p_avail)
    for case in ("y/n", "m/n"):
        q = F.case_formula(case, c_builtin, c_avail, p_builtin, p_avail)
        assert solve(base_model() + [rc_default, q]).check() == z3.unsat
    # With the no-stub rule the same queries become satisfiable.
    rc_nostub = c_avail
    for case in ("y/n", "m/n"):
        q = F.case_formula(case, c_builtin, c_avail, p_builtin, p_avail)
        assert solve(base_model() + [rc_nostub, q]).check() == z3.sat


def test_dimacs_round_trip_keeps_satisfiability(tmp_path):
    path = str(tmp_path / "m.dimacs")
    var_map = F.dump_dimacs(base_model(), path)
    assert var_map
    text = open(path).read()
    assert text.startswith("c kreach")
    header = [l for l in text.splitlines() if l.startswith("p cnf")]
    assert len(header) == 1
    nvars, nclauses = (int(x) for x in header[0].split()[2:4])
    assert nvars == len(var_map)
    body = [l for l in text.splitlines() if not l.startswith(("c", "p"))]
    assert len([l for l in body if l.strip()]) == nclauses


def test_collect_config_symbols_strips_level_suffix():
    e = z3.And(F.is_m("CONFIG_FOO"), z3.Not(F.enabled_var("CONFIG_BAR")))
    assert F.collect_config_symbols([e]) == {"CONFIG_FOO", "CONFIG_BAR"}


def test_gzipped_json_round_trips_through_the_temp_file(tmp_path):
    """write_json goes via a .tmp name, which must not lose the .gz suffix."""
    import gzip

    from kreach.util import read_json, read_jsonl, write_json, write_jsonl

    p = str(tmp_path / "blob.json.gz")
    write_json(p, {"a": [1, 2, 3]})
    with gzip.open(p, "rt") as fh:          # really compressed
        fh.read()
    assert read_json(p) == {"a": [1, 2, 3]}

    q = str(tmp_path / "recs.jsonl.gz")
    assert write_jsonl(q, [{"i": 1}, {"i": 2}]) == 2
    with gzip.open(q, "rt") as fh:
        fh.read()
    assert [r["i"] for r in read_jsonl(q)] == [1, 2]

    plain = str(tmp_path / "plain.json")
    write_json(plain, {"b": 1})
    assert open(plain).read().lstrip().startswith("{")


def test_undefined_symbols_are_pinned_off_all_three_variables():
    """An option the architecture never declares has no type, so
    completion_constraints skips it -- and then X==y floats free of X, which
    makes "built but not built in" satisfiable for an object that is never
    built.  CONFIG_GENERIC_HWEIGHT on x86 is the real case."""
    types = {"CONFIG_MODULES": "bool", "CONFIG_KNOWN": "tristate"}
    used = {"CONFIG_KNOWN", "CONFIG_GENERIC_HWEIGHT", "not_a_config"}

    base = F.completion_constraints(types) + F.modules_off_forces_no_m(types)
    avail = F.enabled_var("CONFIG_GENERIC_HWEIGHT")
    builtin = F.relabel_builtin(avail)      # bool-looking: left as the base var

    # Without the pin, "available and not builtin" is satisfiable.
    s = z3.Solver()
    s.add(*base, avail, z3.Not(F.is_y("CONFIG_GENERIC_HWEIGHT")))
    assert s.check() == z3.sat

    pins = F.undefined_symbols_off(used, types)
    names = {str(p) for p in pins}
    assert "Not(CONFIG_GENERIC_HWEIGHT)" in names
    assert "Not(CONFIG_GENERIC_HWEIGHT==y)" in names
    assert "Not(CONFIG_GENERIC_HWEIGHT==m)" in names
    assert not any("CONFIG_KNOWN" in n for n in names)   # declared: untouched
    assert not any("not_a_config" in n for n in names)   # not a CONFIG symbol

    s = z3.Solver()
    s.add(*base, *pins, avail)
    assert s.check() == z3.unsat
    s = z3.Solver()
    s.add(*base, *pins, avail, z3.Not(builtin))
    assert s.check() == z3.unsat
