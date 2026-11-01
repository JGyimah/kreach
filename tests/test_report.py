"""S7 picks the fix from the provider's role, and must never suggest select."""

from kreach.config import Context
from kreach.s7_report import (FIX_DEPENDS, FIX_DEPENDS_OR_NOT, build_findings,
                              compare, detect_stub, group_by_caller,
                              render_text, suggest_fix)
from kreach.util import write_json, write_jsonl


STUBBED_HEADER = """\
#ifndef _BAR_H
#define _BAR_H
#if IS_ENABLED(CONFIG_BAR)
int bar_register(struct bar *b);
void bar_unregister(struct bar *b);
#else
static inline int bar_register(struct bar *b) { return -ENODEV; }
static inline void bar_unregister(struct bar *b) { }
#endif
#endif
"""

BARE_HEADER = """\
#ifndef _BAZ_H
#define _BAZ_H
int baz_register(struct baz *b);
#endif
"""


def make_tree(tmp_path):
    inc = tmp_path / "include" / "linux"
    inc.mkdir(parents=True)
    (inc / "bar.h").write_text(STUBBED_HEADER)
    (inc / "baz.h").write_text(BARE_HEADER)
    return str(tmp_path)


def test_detect_stub_finds_the_is_enabled_pattern(tmp_path):
    ksrc = make_tree(tmp_path)
    got = detect_stub(ksrc, ["bar_register"], "CONFIG_BAR")
    assert got["stub"] is True
    assert "bar.h" in got["header"]


def test_detect_stub_reports_absence(tmp_path):
    ksrc = make_tree(tmp_path)
    got = detect_stub(ksrc, ["baz_register"], "CONFIG_BAZ")
    assert got["stub"] is False


def test_ym_with_a_stub_suggests_depends_on_or_not(tmp_path):
    ksrc = make_tree(tmp_path)
    rec = {"case": "y/m", "caller_option": "CONFIG_FOO",
           "provider_option": "CONFIG_BAR", "symbols": ["bar_register"]}
    fix = suggest_fix(rec, ksrc)
    assert fix["pattern"] == FIX_DEPENDS_OR_NOT
    assert fix["kconfig"] == "depends on BAR || !BAR"
    assert fix["where"] == "config FOO"
    assert fix["last_resort"] is False


def test_no_stub_suggests_plain_depends_on(tmp_path):
    ksrc = make_tree(tmp_path)
    rec = {"case": "y/m", "caller_option": "CONFIG_FOO",
           "provider_option": "CONFIG_BAZ", "symbols": ["baz_register"]}
    fix = suggest_fix(rec, ksrc)
    assert fix["pattern"] == FIX_DEPENDS
    assert fix["kconfig"] == "depends on BAZ"


def test_mn_case_suggests_depends_on(tmp_path):
    ksrc = make_tree(tmp_path)
    rec = {"case": "m/n", "caller_option": "CONFIG_FOO",
           "provider_option": "CONFIG_BAR", "symbols": ["bar_register"]}
    assert suggest_fix(rec, ksrc)["pattern"] == FIX_DEPENDS


def test_select_is_never_suggested(tmp_path):
    ksrc = make_tree(tmp_path)
    for case in ("y/m", "y/n", "m/n"):
        for sym, opt in (("bar_register", "CONFIG_BAR"), ("baz_register", "CONFIG_BAZ")):
            fix = suggest_fix({"case": case, "caller_option": "CONFIG_FOO",
                               "provider_option": opt, "symbols": [sym]}, ksrc)
            assert "select" not in fix["kconfig"]
            assert "select" not in fix["rationale"]


def confirmed_record(**over):
    rec = {
        "status": "confirmed", "id": "abc123", "case": "y/m", "arch": "x86_64",
        "caller": "drivers/foo/foo_core.o", "caller_option": "CONFIG_FOO",
        "caller_source": "drivers/foo/foo_core.c",
        "provider": "drivers/bar/bar.o", "provider_option": "CONFIG_BAR",
        "provider_module": "bar", "symbols": ["bar_register", "bar_unregister"],
        "failing_symbols": ["bar_register"],
        "witness": "s5/witness/abc123.config",
        "build_log": "s6/build/abc123/build.log",
        "build_log_lines": ["ld: undefined reference to `bar_register'"],
        "levels": {"CONFIG_FOO": "y", "CONFIG_BAR": "m"},
        "build_seconds": 212.0,
    }
    rec.update(over)
    return rec


def test_findings_hold_every_field_the_spec_lists(tmp_path):
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.12-rc3",
                                       "kernel_commit": "deadbeef"})
    findings = build_findings(ctx, [confirmed_record()])
    assert len(findings) == 1
    f = findings[0]
    for field in ("id", "arch", "case", "caller", "provider", "symbols",
                  "witness", "build_log", "fix", "kernel"):
        assert field in f, field
    # build_log holds the failing lines, as the spec's record does; the file
    # they came from sits beside it.
    assert f["build_log"] == ["ld: undefined reference to `bar_register'"]
    assert f["build_log_path"] == "s6/build/abc123/build.log"
    assert f["provider"]["module"] == "bar.ko"
    assert f["kernel"] == "v6.12-rc3"


def test_rejected_records_never_become_findings(tmp_path):
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.12-rc3"})
    rejected = [confirmed_record(status=s) for s in
                ("rejected_build_ok", "rejected_other_error",
                 "rejected_other_undefined", "build_error", "no_witness")]
    assert build_findings(ctx, rejected) == []


def test_report_groups_by_caller_option_so_one_report_is_one_patch(tmp_path):
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.12-rc3"})
    recs = [confirmed_record(),
            confirmed_record(id="def456", caller="drivers/foo/foo_x.o",
                             provider_option="CONFIG_QUX", symbols=["qux_init"],
                             failing_symbols=["qux_init"]),
            confirmed_record(id="ghi789", caller_option="CONFIG_OTHER",
                             caller="drivers/other/o.o")]
    findings = build_findings(ctx, recs)
    groups = group_by_caller(findings)
    assert sorted(groups) == ["CONFIG_FOO", "CONFIG_OTHER"]
    assert len(groups["CONFIG_FOO"]) == 2
    text = render_text(ctx, findings)
    assert "## CONFIG_FOO  (2 findings)" in text
    assert "commit message reproduction section" in text
    assert "undefined reference to `bar_register'" in text


def test_compare_marks_still_present_fixed_and_new(tmp_path):
    prev = str(tmp_path / "prev.jsonl")
    cur = str(tmp_path / "cur.jsonl")

    def rec(i):
        return {"id": i, "case": "y/m", "caller": {"option": "CONFIG_%s" % i},
                "provider": {"option": "CONFIG_BAR"}, "symbols": ["s"],
                "kernel": "v6.12"}

    write_jsonl(prev, [rec("a"), rec("b")])
    write_jsonl(cur, [rec("b"), rec("c")])
    out = compare(prev, cur)
    assert out["counts"] == {"still_present": 1, "fixed": 1, "new": 1}
    assert out["still_present"][0]["id"] == "b"
    assert out["fixed"][0]["id"] == "a"
    assert out["new"][0]["id"] == "c"


def test_commit_block_covers_every_provider_in_the_group(tmp_path):
    """One report is one patch, so it must mention all the missing
    dependencies of that caller, not just the first."""
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.17"})
    recs = [confirmed_record(),
            confirmed_record(id="def456", provider_option="CONFIG_BAZ",
                             symbols=["baz_register"], failing_symbols=["baz_register"],
                             build_log_lines=["ld: undefined reference to `baz_register'"])]
    text = render_text(ctx, build_findings(ctx, recs))
    block = text[text.index("commit message reproduction"):]
    assert "CONFIG_BAR" in block and "CONFIG_BAZ" in block
    assert "depends on BAR || !BAR" in block
    assert "depends on BAZ" in block
    assert "configurations (x86_64" in block     # plural


def test_commit_block_for_a_single_finding_stays_singular(tmp_path):
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.17"})
    text = render_text(ctx, build_findings(ctx, [confirmed_record()]))
    block = text[text.index("commit message reproduction"):]
    assert "configuration (x86_64" in block
    assert "bar_register is" in block


def test_a_provider_with_no_prompt_gets_a_caveat_not_a_select(tmp_path):
    """CONFIG_BITREVERSE has no prompt; it exists to be selected.  The spec
    rules out suggesting select, so the report says plainly that 'depends on'
    alone will not work here."""
    ksrc = make_tree(tmp_path)
    rec = {"case": "y/m", "caller_option": "CONFIG_SND_SOC_FSL_XCVR",
           "provider_option": "CONFIG_BITREVERSE", "symbols": ["byte_rev_table"]}
    fix = suggest_fix(rec, ksrc, visible={"CONFIG_SND_SOC_FSL_XCVR"})
    assert "select" not in fix["kconfig"]
    assert fix["caveat"] is not None
    assert "no prompt" in fix["caveat"]
    assert "unselectable" in fix["caveat"]


def test_a_visible_provider_gets_no_caveat(tmp_path):
    ksrc = make_tree(tmp_path)
    rec = {"case": "y/m", "caller_option": "CONFIG_FOO",
           "provider_option": "CONFIG_BAR", "symbols": ["bar_register"]}
    fix = suggest_fix(rec, ksrc, visible={"CONFIG_FOO", "CONFIG_BAR"})
    assert fix["caveat"] is None


def test_caveat_reaches_the_commit_block(tmp_path):
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.17"})
    write_json(ctx.vars_file, {"visible": ["CONFIG_FOO"], "types": {}})
    text = render_text(ctx, build_findings(ctx, [confirmed_record()]))
    assert "caveat" in text
    assert "Note: BAR has no prompt" in text


def test_module_name_uses_the_build_data_when_it_has_it():
    from kreach.s7_report import module_name

    rec = confirmed_record()           # provider_module "bar" from .mod files
    assert module_name(rec) == "bar.ko"
    assert module_name(confirmed_record(provider_module="baz.ko")) == "baz.ko"


def test_module_name_falls_back_to_the_object_for_a_provider_outside_the_build():
    """lib/bitrev.o is a single-file module and sits outside the sound/ build,
    so S3 has no .mod entry for it -- but Kbuild names it after the object."""
    from kreach.s7_report import module_name

    rec = confirmed_record(provider="lib/bitrev.o", provider_module=None,
                           provider_option="CONFIG_BITREVERSE",
                           levels={"CONFIG_BITREVERSE": "m",
                                   "CONFIG_SND_SOC_FSL_XCVR": "y"})
    assert module_name(rec) == "bitrev.ko"


def test_module_name_is_none_when_the_provider_is_not_a_module():
    """A y/n finding has the provider off, so there is no .ko to name."""
    from kreach.s7_report import module_name

    rec = confirmed_record(case="y/n", provider="lib/bitrev.o",
                           provider_module=None,
                           provider_option="CONFIG_BITREVERSE",
                           levels={"CONFIG_BITREVERSE": "n"})
    assert module_name(rec) is None


def test_fixes_tag_and_maintainers_are_absent_without_a_source(tmp_path):
    """Both lookups need the caller's source file; neither may crash on an
    object with no .c beside it."""
    from kreach.s7_report import fixes_tag, maintainers

    ksrc = make_tree(tmp_path)
    assert fixes_tag(ksrc, None, ["sym"]) is None
    assert maintainers(ksrc, None) == []
    # No get_maintainer.pl in this tree, and not a git repo.
    assert maintainers(ksrc, "drivers/foo/foo.c") == []
    assert fixes_tag(ksrc, "drivers/foo/foo.c", ["sym"]) is None


def test_fixes_lookup_is_cached_per_source_and_symbols(tmp_path):
    from kreach.s7_report import fixes_tag, maintainers

    ksrc = make_tree(tmp_path)
    fcache, mcache = {}, {}
    fixes_tag(ksrc, "a.c", ["s"], cache=fcache)
    maintainers(ksrc, "a.c", cache=mcache)
    assert ("a.c", ("s",)) in fcache
    assert "a.c" in mcache


def test_findings_carry_the_patch_conventions_fields(tmp_path):
    """The spec's patch conventions want a Fixes: tag and the caller's
    maintainers, so the record carries both even when they come out empty."""
    ksrc = make_tree(tmp_path)
    ctx = Context(ksrc=ksrc, work=str(tmp_path / "work"), arch="x86_64")
    write_json(ctx.path("meta.json"), {"kernel_describe": "v6.17"})
    f = build_findings(ctx, [confirmed_record()])[0]
    assert "fixes" in f
    assert "maintainers" in f
