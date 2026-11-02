# SPDX-License-Identifier: GPL-2.0-or-later
"""S4, S5 and S6: candidates, queries and verdicts.

S4's guard refinement and S6's build-log classification are the two places
where a wrong answer costs precision, so both are pinned down against text the
kernel really produces.
"""

import z3

from kreach import formula as F
from kreach.s2_presence import PresenceTable
from kreach.s4_refcond import (RC_DEFAULT, RC_NO_STUB, SourceIndex, _guard_near,
                               _merge_by_pair, _pick_option, rc_formula)
from kreach.s6_confirm import _scan_log


GUARDED_SITE = """\
#include <linux/bar.h>
int foo_probe(void)
{
        if (IS_REACHABLE(CONFIG_BAR)) {
                return bar_register(&b);
        }
        return 0;
}
"""

UNGUARDED_SITE = """\
#include <linux/bar.h>
int foo_probe(void)
{
        return bar_register(&b);
}
"""


def _table(avail_c, avail_p):
    def smt2(e):
        s = z3.Solver()
        s.add(e)
        return s.to_smt2()

    data = {
        "c.o": {"available": smt2(avail_c),
                "builtin": smt2(F.relabel_builtin(avail_c)),
                "own_symbols": ["CONFIG_C"], "all_symbols": ["CONFIG_C"], "module": None},
        "p.o": {"available": smt2(avail_p),
                "builtin": smt2(F.relabel_builtin(avail_p)),
                "own_symbols": ["CONFIG_P"], "all_symbols": ["CONFIG_P"], "module": "p"},
    }
    return PresenceTable(data, set(), set())


def test_guard_near_finds_is_reachable(tmp_path):
    p = tmp_path / "foo.c"
    p.write_text(GUARDED_SITE)
    got = _guard_near(p.read_text().splitlines(), "bar_register")
    assert got is not None
    macro, sym, _lineno = got
    assert (macro, sym) == ("IS_REACHABLE", "CONFIG_BAR")


def test_guard_near_ignores_plain_call_sites(tmp_path):
    p = tmp_path / "foo.c"
    p.write_text(UNGUARDED_SITE)
    assert _guard_near(p.read_text().splitlines(), "bar_register") is None


def test_is_builtin_also_counts_as_guarded(tmp_path):
    p = tmp_path / "foo.c"
    p.write_text(GUARDED_SITE.replace("IS_REACHABLE", "IS_BUILTIN"))
    macro, sym, _ = _guard_near(p.read_text().splitlines(), "bar_register")
    assert macro == "IS_BUILTIN"


def test_source_for_object_maps_o_back_to_c(tmp_path):
    (tmp_path / "drivers").mkdir()
    (tmp_path / "drivers" / "foo.c").write_text("int x;\n")
    idx = SourceIndex(str(tmp_path))
    assert idx.source_for_object("drivers/foo.o") == "drivers/foo.c"
    assert idx.source_for_object("drivers/missing.o") is None


def test_pick_option_prefers_the_most_specific_tristate():
    types = {"CONFIG_SND": "bool", "CONFIG_SND_SOC_FOO": "tristate",
             "CONFIG_SND_SOC": "tristate"}
    got = _pick_option(["CONFIG_SND", "CONFIG_SND_SOC", "CONFIG_SND_SOC_FOO"], types)
    assert got == "CONFIG_SND_SOC_FOO"


def test_merge_by_pair_collects_all_symbols_of_one_pair():
    cands = [
        {"caller": "a.o", "provider": "b.o", "symbol": "s2",
         "caller_option": "CONFIG_A", "provider_option": "CONFIG_B"},
        {"caller": "a.o", "provider": "b.o", "symbol": "s1",
         "caller_option": "CONFIG_A", "provider_option": "CONFIG_B"},
        {"caller": "a.o", "provider": "c.o", "symbol": "s3",
         "caller_option": "CONFIG_A", "provider_option": "CONFIG_C"},
    ]
    merged = _merge_by_pair(cands)
    assert len(merged) == 2
    bypair = {(m["caller"], m["provider"]): m["symbols"] for m in merged}
    assert bypair[("a.o", "b.o")] == ["s1", "s2"]
    assert bypair[("a.o", "c.o")] == ["s3"]


def test_rc_default_requires_the_provider_enabled():
    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    rc, _ = rc_formula({"caller": "c.o", "provider": "p.o",
                        "rc_rule": RC_DEFAULT}, table, "y/m")
    s = z3.Solver()
    s.add(rc, z3.Not(F.enabled_var("CONFIG_P")))
    assert s.check() == z3.unsat


def test_rc_no_stub_rule_drops_the_provider_requirement():
    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    rc, _ = rc_formula({"caller": "c.o", "provider": "p.o",
                        "rc_rule": RC_NO_STUB}, table, "y/n")
    s = z3.Solver()
    s.add(rc, z3.Not(F.enabled_var("CONFIG_P")))
    assert s.check() == z3.sat


# ------------------------------------------------------------------ S2 ------


def test_query_id_separates_queries_that_share_a_finding_id():
    """Same options and symbols, different presence conditions: one finding
    id, but two witnesses, so they must not write to the same file."""
    from kreach.s5_solve import finding_id, query_id

    a = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    b = _table(z3.And(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_EXTRA")),
               F.enabled_var("CONFIG_P"))
    cand = {"caller": "c.o", "provider": "p.o", "caller_option": "CONFIG_C",
            "provider_option": "CONFIG_P", "symbols": ["s"], "rc_rule": "default"}
    assert (finding_id("CONFIG_C", "CONFIG_P", ["s"])
            == finding_id("CONFIG_C", "CONFIG_P", ["s"]))
    assert query_id(cand, a) != query_id(cand, b)
    assert query_id(cand, a) == query_id(cand, a)


def test_finding_condition_matches_the_case_it_came_from():
    """The randconfig comparison asks the same question S5 asked, against a
    concrete config instead of the whole model."""
    from kreach.randcompare import finding_condition

    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    types = {"CONFIG_MODULES": "bool", "CONFIG_C": "tristate", "CONFIG_P": "tristate"}
    base = (F.completion_constraints(types) + F.modules_off_forces_no_m(types)
            + [F.enabled_var("CONFIG_MODULES")])
    f = {"case": "y/m", "caller": {"object": "c.o"}, "provider": {"object": "p.o"}}
    cond = finding_condition(f, table)

    def sat(extra):
        s = z3.Solver()
        s.add(*base, cond, *extra)
        return s.check() == z3.sat

    assert sat([F.is_y("CONFIG_C"), F.is_m("CONFIG_P")])      # the failing config
    assert not sat([F.is_y("CONFIG_C"), F.is_y("CONFIG_P")])  # provider built in
    assert not sat([F.is_m("CONFIG_C"), F.is_m("CONFIG_P")])  # caller not built in
    assert not sat([F.is_y("CONFIG_C"), z3.Not(F.enabled_var("CONFIG_P"))])  # y/n, not y/m


def test_finding_condition_is_none_for_an_object_outside_the_table():
    from kreach.randcompare import finding_condition

    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    f = {"case": "y/m", "caller": {"object": "missing.o"},
         "provider": {"object": "p.o"}}
    assert finding_condition(f, table) is None


def test_scan_log_reads_a_real_vmlinux_link_error():
    text = ("  LD      vmlinux\n"
            "ld: drivers/foo/foo_core.o: in function `foo_probe':\n"
            "foo_core.c:(.text+0x3f): undefined reference to `bar_register'\n"
            "ld: foo_core.c:(.text+0x5a): undefined reference to `bar_unregister'\n"
            "make: *** [Makefile:1234: vmlinux] Error 1\n")
    undef, modpost = _scan_log(text)
    assert undef == {"bar_register", "bar_unregister"}
    assert modpost == set()


def test_scan_log_reads_a_real_modpost_error():
    text = ('ERROR: modpost: "bar_register" [drivers/foo/foo.ko] undefined!\n'
            "make[2]: *** [scripts/Makefile.modpost:145: modules-only.symvers] Error 1\n")
    undef, modpost = _scan_log(text)
    assert modpost == {"bar_register"}


def test_scan_log_finds_nothing_in_a_clean_build():
    undef, modpost = _scan_log("  LD      vmlinux\n  SORTTAB vmlinux\n")
    assert not undef and not modpost


# --- S5: a witness has to satisfy the whole query, not just two options ---

def test_check_query_holds_rejects_a_config_with_a_guard_turned_off():
    """The real case: the caller is sdca_hid.o, whose presence condition needs
    CONFIG_SND_SOC_SDCA_HID as well as CONFIG_SND_SOC_SDCA.  With both named
    options right but that guard off, nothing is compiled and the build
    succeeds -- which cost a ten-minute link to find out."""
    from kreach.s5_solve import check_query_holds

    class FakeModel:
        kconfig_types = {"CONFIG_MODULES": "bool",
                         "CONFIG_SND_SOC_SDCA": "tristate",
                         "CONFIG_SND_SOC_SDCA_HID": "bool",
                         "CONFIG_HID": "tristate"}

    caller_avail = z3.And(F.enabled_var("CONFIG_SND_SOC_SDCA"),
                          F.enabled_var("CONFIG_SND_SOC_SDCA_HID"))
    caller_builtin = F.relabel_builtin(caller_avail)
    prov_avail = F.enabled_var("CONFIG_HID")
    prov_builtin = F.relabel_builtin(prov_avail)
    query = z3.And(caller_avail, prov_avail,
                   F.case_formula("y/m", caller_builtin, caller_avail,
                                  prov_builtin, prov_avail))

    m = FakeModel()
    good = {"CONFIG_MODULES": "y", "CONFIG_SND_SOC_SDCA": "y",
            "CONFIG_SND_SOC_SDCA_HID": "y", "CONFIG_HID": "m"}
    ok, _ = check_query_holds(good, m, query)
    assert ok

    # Both named options still right, the guard silently off.
    defanged = dict(good, CONFIG_SND_SOC_SDCA_HID="n")
    ok, detail = check_query_holds(defanged, m, query)
    assert not ok
    assert detail["result"] == "unsat"


def test_check_levels_only_looks_at_the_symbols_it_is_given():
    from kreach.s5_solve import check_levels

    kept, detail = check_levels({"CONFIG_A": "y", "CONFIG_B": "m"},
                                {"CONFIG_A": "y", "CONFIG_B": "m"})
    assert kept and not detail["mismatch"]

    kept, detail = check_levels({"CONFIG_A": "m"},
                                {"CONFIG_A": "y", "CONFIG_B": "m"})
    assert not kept
    assert detail["mismatch"] == {"CONFIG_A": ("y", "m"), "CONFIG_B": ("m", "n")}


def test_s6_preflight_skips_a_defanged_witness_without_building(tmp_path):
    """S6 re-checks each witness against its query before spending a build;
    the SDCA/HID case cost 602 seconds to discover the hard way."""
    from kreach.config import Context
    from kreach.s6_confirm import witness_still_exercises

    class FakeModel:
        kconfig_types = {"CONFIG_MODULES": "bool", "CONFIG_C": "tristate",
                         "CONFIG_GUARD": "bool", "CONFIG_P": "tristate"}

    ctx = Context(ksrc=str(tmp_path), work=str(tmp_path / "w"), arch="x86_64")
    table = _table(z3.And(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_GUARD")),
                   F.enabled_var("CONFIG_P"))
    rec = {"case": "y/m", "caller": "c.o", "provider": "p.o",
           "witness": "w.config", "id": "x", "query_id": "x"}

    def write(text):
        path = ctx.path("w.config")
        open(path, "w").write(text)

    write("CONFIG_MODULES=y\nCONFIG_C=y\nCONFIG_GUARD=y\nCONFIG_P=m\n")
    ok, why = witness_still_exercises(ctx, rec, FakeModel(), table)
    assert ok, why

    # Caller and provider levels unchanged; the guard quietly off.
    write("CONFIG_MODULES=y\nCONFIG_C=y\n# CONFIG_GUARD is not set\nCONFIG_P=m\n")
    ok, why = witness_still_exercises(ctx, rec, FakeModel(), table)
    assert not ok
    assert "no longer satisfies the query" in why

    # With no model or table there is nothing to check against, and S6 builds.
    assert witness_still_exercises(ctx, rec, None, None)[0] is True


def test_a_build_killed_by_a_signal_is_not_a_rejection():
    """rc < 0 means the machine was interrupted, not that the finding is
    false.  Recording it as a rejection would drop a real finding and feed a
    false entry into S4's feedback loop."""
    from kreach.s6_confirm import TERMINAL

    assert "build_interrupted" not in TERMINAL
    assert "build_error" not in TERMINAL
    # The verdicts that do settle a witness.
    assert "confirmed" in TERMINAL
    assert "rejected_build_ok" in TERMINAL
    assert "rejected_other_error" in TERMINAL
    assert "skipped_witness_defanged" in TERMINAL


def test_unresolved_results_are_retried_and_terminal_ones_are_not():
    from kreach.s6_confirm import TERMINAL, _key

    records = [
        {"id": "a", "query_id": "a", "case": "y/m", "status": "confirmed"},
        {"id": "b", "query_id": "b", "case": "y/m", "status": "build_interrupted"},
        {"id": "c", "query_id": "c", "case": "y/m", "status": "rejected_build_ok"},
    ]
    keep = {_key(r): r for r in records if r.get("status") in TERMINAL}
    assert set(k[0] for k in keep) == {"a", "c"}


def test_feedback_only_carries_verdicts_s6_actually_reached(tmp_path):
    """An interrupted build says nothing about the pair.  Feeding it back as a
    rejection would drop the pair from every later run on the strength of a
    machine hiccup."""
    from kreach.config import Context
    from kreach.s4_refcond import feedback_from_s6
    from kreach.util import write_jsonl

    ctx = Context(ksrc=str(tmp_path), work=str(tmp_path / "w"), arch="x86_64")
    write_jsonl(ctx.confirm_file, [
        {"status": "confirmed", "caller_option": "CONFIG_A",
         "provider_option": "CONFIG_P", "symbols": ["keep_me"]},
        {"status": "rejected_build_ok", "caller_option": "CONFIG_B",
         "provider_option": "CONFIG_P", "symbols": ["drop_me"]},
        {"status": "skipped_witness_defanged", "caller_option": "CONFIG_C",
         "provider_option": "CONFIG_P", "symbols": ["drop_me_too"]},
        {"status": "build_interrupted", "caller_option": "CONFIG_D",
         "provider_option": "CONFIG_P", "symbols": ["retry_me"]},
        {"status": "build_error", "caller_option": "CONFIG_E",
         "provider_option": "CONFIG_P", "symbols": ["retry_me_too"]},
    ])
    pairs = feedback_from_s6(ctx)["rejected_pairs"]
    assert "CONFIG_B|CONFIG_P|drop_me" in pairs
    assert "CONFIG_C|CONFIG_P|drop_me_too" in pairs
    # Confirmed findings are not rejections, and unresolved ones are not either.
    assert not any("keep_me" in p for p in pairs)
    assert not any("retry_me" in p for p in pairs)
    assert not any("retry_me_too" in p for p in pairs)


def test_reference_check_is_skipped_when_there_is_nothing_to_check(tmp_path):
    """An inconclusive pre-check must never drop a finding: it returns True so
    the full build still happens."""
    from kreach.config import Context
    from kreach.s6_confirm import reference_still_emitted

    ctx = Context(ksrc=str(tmp_path), work=str(tmp_path / "w"), arch="x86_64")

    # No symbols to look for.
    ok, why = reference_still_emitted(
        ctx, {"caller": "a.o", "symbols": [], "witness": "w.config"})
    assert ok and "nothing to check" in why

    # Caller is not an object file.
    ok, why = reference_still_emitted(
        ctx, {"caller": "a/", "symbols": ["s"], "witness": "w.config"})
    assert ok and "nothing to check" in why

    # Witness is missing.
    ok, why = reference_still_emitted(
        ctx, {"caller": "a.o", "symbols": ["s"], "witness": "gone.config"})
    assert ok and "no witness" in why


def test_nm_undefined_keeps_only_the_undefined_entries(monkeypatch):
    from kreach import s6_confirm

    rows = [("a.o", "U", "wanted"), ("a.o", "T", "defined_here"),
            ("a.o", "W", "weak_one"), ("a.o", "U", "also_wanted")]
    monkeypatch.setattr("kreach.s3_symbols._nm_batch", lambda paths: iter(rows))
    got = {name for _p, _t, name in s6_confirm._nm_undefined(["a.o"])}
    assert got == {"wanted", "also_wanted"}


def test_reference_not_emitted_is_a_terminal_verdict():
    """It is a real answer -- the link cannot fail on that symbol -- so it
    settles the witness and feeds back into S4."""
    from kreach.s4_refcond import S6_REJECTIONS
    from kreach.s6_confirm import TERMINAL

    assert "skipped_reference_not_emitted" in TERMINAL
    assert "skipped_reference_not_emitted" in S6_REJECTIONS


def test_an_environment_failure_is_not_a_verdict_on_the_finding():
    """A missing generated header or an OOM says something about the machine,
    not about the symbol.  Recording it as a rejection drops a real finding."""
    from kreach.s6_confirm import ENVIRONMENT_FAILURES, TERMINAL

    assert "build_environment_error" not in TERMINAL
    # The signatures actually seen, plus the resource ones worth catching.
    assert any("autoconf.h" in s for s in ENVIRONMENT_FAILURES)
    assert any("No space left" in s for s in ENVIRONMENT_FAILURES)
    assert any("Cannot allocate memory" in s for s in ENVIRONMENT_FAILURES)


def test_environment_failure_signatures_match_real_make_output():
    from kreach.s6_confirm import ENVIRONMENT_FAILURES

    real = ("/home/u/src/linux/include/linux/kconfig.h:5:10: fatal error: "
            "generated/autoconf.h: No such file or directory\n"
            "make[6]: *** [scripts/Makefile.build:287: x2apic_cluster.o] Error 1\n")
    assert [s for s in ENVIRONMENT_FAILURES if s in real]

    clean = ("ld: foo.o: undefined reference to `bar_register'\n"
             "make: *** [Makefile:1234: vmlinux] Error 1\n")
    assert not [s for s in ENVIRONMENT_FAILURES if s in clean]


def test_config_to_assignment_can_be_restricted_to_some_symbols():
    """Fixing all 18,000 symbols is wasted work when the formula mentions a
    handful, and restricting it cannot change the answer because the rest are
    unconstrained."""
    from kreach.s1_kconfig import config_to_assignment

    class FakeModel:
        kconfig_types = {"CONFIG_A": "tristate", "CONFIG_B": "tristate",
                         "CONFIG_N": "int"}

    settings = {"CONFIG_A": "y", "CONFIG_B": "m", "CONFIG_N": "7"}
    full = config_to_assignment(settings, FakeModel())
    part = config_to_assignment(settings, FakeModel(), symbols={"CONFIG_A"})

    names = {str(l) for l in part}
    assert any("CONFIG_A" in n for n in names)
    assert not any("CONFIG_B" in n for n in names)
    assert len(part) < len(full)
    # Non-boolean symbols never produce level literals either way.
    assert not any("CONFIG_N" in str(l) for l in full)

    # Restricting to the symbols a formula mentions gives the same verdict.
    q = z3.And(F.is_y("CONFIG_A"), F.is_m("CONFIG_B"))
    both = config_to_assignment(settings, FakeModel(),
                                symbols=F.collect_config_symbols([q]))
    for lits in (full, both):
        s = z3.Solver()
        s.add(*lits, q)
        assert s.check() == z3.sat


def test_filter_candidates_narrows_by_caller_and_provider():
    """Re-confirming a known set against another revision only needs those
    pairs solved, not the whole sweep."""
    from kreach.s5_solve import filter_candidates

    cands = [
        {"caller_option": "CONFIG_A", "provider_option": "CONFIG_P"},
        {"caller_option": "CONFIG_B", "provider_option": "CONFIG_P"},
        {"caller_option": "CONFIG_A", "provider_option": "CONFIG_Q"},
    ]
    assert len(filter_candidates(cands)) == 3
    assert [c["caller_option"] for c in filter_candidates(cands, callers=["CONFIG_A"])] \
        == ["CONFIG_A", "CONFIG_A"]
    assert len(filter_candidates(cands, providers=["CONFIG_P"])) == 2
    both = filter_candidates(cands, callers=["CONFIG_A"], providers=["CONFIG_Q"])
    assert len(both) == 1 and both[0]["provider_option"] == "CONFIG_Q"
    assert filter_candidates(cands, callers=["CONFIG_NOPE"]) == []


def test_oom_killed_link_is_an_environment_failure_not_a_rejection():
    """make reports a SIGKILLed child as Error 137.  The usual cause is the
    OOM killer taking ld during the vmlinux link, which says nothing about
    the finding -- recording it as a rejection would drop a real bug."""
    from kreach.s6_confirm import ENVIRONMENT_FAILURES

    real = ("  CC      net/9p/error.o\n"
            "make[3]: *** [scripts/Makefile.vmlinux_o:79: vmlinux.o] Error 137\n"
            "make[3]: *** Deleting file 'vmlinux.o'\n")
    assert [s for s in ENVIRONMENT_FAILURES if s in real]

    # A genuine link failure on the predicted symbol must still be a verdict.
    genuine = ("ld: fsl_xcvr.c:(.text+0x1): undefined reference to `byte_rev_table'\n"
               "make: *** [Makefile:1234: vmlinux] Error 1\n")
    assert not [s for s in ENVIRONMENT_FAILURES if s in genuine]


# --- S5: witness size, and the fixed-point shrink -----------------------

def test_mark_size_outliers_flags_the_one_that_is_nothing_like_its_peers():
    """Sizes in the range the v7.3 run produced, plus a synthetic outlier.

    The real run has none to flag: block-removal minimisation brought its
    largest witness down to 1,078 against a median of 700, well inside the
    threshold.  An unminimised one of that shape sat at 3,822 and was the
    reason for this check, so that is the number used here.
    """
    from kreach.s5_solve import mark_size_outliers

    results = [{"status": "sat", "caller_option": "CONFIG_%d" % n,
                "witness_symbols_on": n}
               for n in (505, 506, 511, 512, 696, 697, 700, 828, 841, 842, 3822)]
    mark_size_outliers(results)

    flagged = [r["caller_option"] for r in results if r["witness_size_outlier"]]
    assert flagged == ["CONFIG_3822"]
    assert all(r["witness_median"] == 697 for r in results)


def test_mark_size_outliers_flags_nothing_when_the_sizes_are_alike():
    from kreach.s5_solve import mark_size_outliers

    results = [{"status": "sat", "caller_option": "c%d" % n,
                "witness_symbols_on": n} for n in (500, 600, 700, 800)]
    mark_size_outliers(results)
    assert not any(r["witness_size_outlier"] for r in results)


def test_mark_size_outliers_has_a_floor_so_small_sets_are_not_flagged():
    """Three times a median of 10 is 30, which is not a big build.  Without a
    floor every tiny witness with a slightly larger sibling gets flagged."""
    from kreach.s5_solve import mark_size_outliers

    results = [{"status": "sat", "caller_option": "a", "witness_symbols_on": 10},
               {"status": "sat", "caller_option": "b", "witness_symbols_on": 400}]
    mark_size_outliers(results)
    assert not any(r["witness_size_outlier"] for r in results)


def test_mark_size_outliers_ignores_unsat_and_sizeless_records():
    from kreach.s5_solve import mark_size_outliers

    results = [{"status": "unsat"},
               {"status": "sat"},
               {"status": "sat", "witness_symbols_on": 100, "caller_option": "c"}]
    mark_size_outliers(results)
    assert "witness_size_outlier" not in results[0]
    assert "witness_size_outlier" not in results[1]
    assert results[2]["witness_size_outlier"] is False


def test_minimiser_keeps_shrinking_while_it_can():
    """One pass of the core loop stops at the first satisfying assignment,
    which need not be a small one.  The loop runs to a fixed point."""
    from kreach.s5_solve import Minimiser

    types = {"CONFIG_MODULES": "bool"}
    for n in range(12):
        types["CONFIG_S%d" % n] = "tristate"

    class FakeModel:
        kconfig_types = types
        constraints = (F.completion_constraints(types)
                       + F.modules_off_forces_no_m(types)
                       + [F.enabled_var("CONFIG_MODULES")])

        @property
        def bool_tristate(self):
            return {s for s, t in types.items() if t in ("bool", "tristate")}

    mini = Minimiser(FakeModel())
    # Only S0 is required; everything else should end up off.
    status, model, info = mini.solve(F.enabled_var("CONFIG_S0"))
    assert status == "sat"
    on = {s for s in FakeModel().bool_tristate
          if z3.is_true(model.eval(F.enabled_var(s), model_completion=True))}
    assert "CONFIG_S0" in on
    assert not {s for s in on if s.startswith("CONFIG_S") and s != "CONFIG_S0"}
    # Sizes are recorded, non-increasing, and the pass count is bounded.
    assert info["sizes"] == sorted(info["sizes"], reverse=True)
    assert 1 <= info["passes"] <= mini.max_passes
