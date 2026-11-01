# SPDX-License-Identifier: GPL-2.0-or-later
"""S2 and S3: reading Kbuild.

The presence conditions are where kreach's y/m split is made, and the
acceptance test keeps finding that Kbuild has one more shape than the model
had.  Each test here is pinned to a construct the kernel actually uses.
"""

import z3

from kreach import formula as F
from kreach.s2_presence import PresenceTable, scan_makefile_guards


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


def test_scan_makefile_guards_separates_ifdef_from_exact_tests(tmp_path):
    (tmp_path / "Makefile").write_text(
        "ifdef CONFIG_GUARDED\n"
        "obj-$(CONFIG_SEL) += sel.o\n"
        "endif\n"
        "ifeq ($(CONFIG_EXACT_Y),y)\n"
        "obj-y += ey.o\n"
        "endif\n"
        "ifeq ($(CONFIG_EXACT_M),m)\n"
        "obj-m += em.o\n"
        "endif\n"
        "ifneq ($(CONFIG_NONEMPTY),)\n"
        "obj-y += ne.o\n"
        "endif\n")
    enabled, exact_m, notes = scan_makefile_guards(str(tmp_path))
    assert "CONFIG_GUARDED" in enabled
    assert "CONFIG_NONEMPTY" in enabled
    assert "CONFIG_EXACT_Y" not in enabled   # exact-y: relabelling is right
    assert exact_m == {"CONFIG_EXACT_M"}
    assert any(n["kind"] == "ifeq-m" for n in notes)


# ------------------------------------------------------------------ S6 ------


def test_scan_exact_y_scopes_attributes_objects_to_their_block(tmp_path):
    """sound/Makefile's real shape: CONFIG_SND is an ordinary selector for the
    subdirectories and an exact-y test for last.o, two lines apart."""
    from kreach.s2_presence import exact_y_symbols_for, scan_exact_y_scopes

    (tmp_path / "sound").mkdir()
    (tmp_path / "sound" / "Makefile").write_text(
        "obj-$(CONFIG_SOUND) += soundcore.o\n"
        "obj-$(CONFIG_SND) += core/ pci/\n"
        "\n"
        "ifeq ($(CONFIG_SND),y)\n"
        "  obj-y += last.o\n"
        "endif\n"
        "\n"
        "ifeq ($(CONFIG_OTHER),y)\n"
        "  obj-y += inner/\n"
        "else\n"
        "  obj-y += notinner.o\n"
        "endif\n")
    objs, dirs = scan_exact_y_scopes(str(tmp_path), ["sound"])

    assert objs["sound/last.o"] == {"CONFIG_SND"}
    # The ordinary selector two lines above must not be caught.
    assert "sound/soundcore.o" not in objs
    # A directory inside the block applies to everything beneath it.
    assert dirs["sound/inner/"] == {"CONFIG_OTHER"}
    assert exact_y_symbols_for("sound/inner/deep/thing.o", objs, dirs) == {"CONFIG_OTHER"}
    # The else branch is the negation, so it carries nothing.
    assert "sound/notinner.o" not in objs
    assert exact_y_symbols_for("sound/soundcore.o", objs, dirs) == set()


def test_exact_y_symbol_blocks_the_module_level_entirely():
    """With CONFIG_SND=m, sound/last.o is not built at all -- not built as a
    module.  That is what the available condition has to say."""
    import z3 as _z3

    types = {"CONFIG_MODULES": "bool", "CONFIG_SND": "tristate"}
    base = (F.completion_constraints(types) + F.modules_off_forces_no_m(types)
            + [F.enabled_var("CONFIG_MODULES")])
    plain = F.enabled_var("CONFIG_SND")                 # ordinary selector
    exact = F.relabel_builtin(plain)                    # inside ifeq(...,y)

    def sat(cond, level):
        s = _z3.Solver()
        s.add(*base, cond, level)
        return s.check() == _z3.sat

    assert sat(plain, F.is_m("CONFIG_SND"))       # other sound objects: built as m
    assert not sat(exact, F.is_m("CONFIG_SND"))   # last.o: not built at all
    assert sat(exact, F.is_y("CONFIG_SND"))


def test_scan_selector_scopes_finds_the_obj_assignment_symbols(tmp_path):
    """sound/soc/Makefile uses one symbol as an ifneq guard and as an
    obj-$() selector on adjacent lines.  The selector reading has to win."""
    from kreach.s2_presence import (exact_y_symbols_for, scan_makefile_guards,
                                    scan_selector_scopes)

    (tmp_path / "soc").mkdir()
    (tmp_path / "soc" / "Makefile").write_text(
        "ifneq ($(CONFIG_SND_SOC_ACPI),)\n"
        "obj-$(CONFIG_SND_SOC_ACPI) += soc-acpi.o\n"
        "endif\n"
        "ifdef CONFIG_PURE_GUARD\n"
        "obj-y += always.o\n"
        "endif\n"
        "obj-$(CONFIG_SUB) += sub/\n")

    enabled, _exact_m, _notes = scan_makefile_guards(str(tmp_path))
    assert "CONFIG_SND_SOC_ACPI" in enabled      # it *is* used as a guard
    assert "CONFIG_PURE_GUARD" in enabled

    objs, dirs = scan_selector_scopes(str(tmp_path), ["soc"])
    assert objs["soc/soc-acpi.o"] == {"CONFIG_SND_SOC_ACPI"}
    # A pure guard selects nothing, so it keeps its enabled reading.
    assert "soc/always.o" not in objs
    assert dirs["soc/sub/"] == {"CONFIG_SUB"}
    assert exact_y_symbols_for("soc/sub/deep.o", objs, dirs) == {"CONFIG_SUB"}

    # The guard protection is removed only for the object it selects.
    assert (enabled - objs["soc/soc-acpi.o"]) == {"CONFIG_PURE_GUARD"}


def test_composite_parts_inherit_their_composites_selector(tmp_path):
    """snd-soc-acpi-y := soc-acpi.o carries no CONFIG symbol; the selector is
    on obj-$(CONFIG_SND_SOC_ACPI) += snd-soc-acpi.o."""
    from kreach.s2_presence import (_module_name_hints, exact_y_symbols_for,
                                    scan_selector_scopes)

    (tmp_path / "soc").mkdir()
    (tmp_path / "soc" / "Makefile").write_text(
        "ifneq ($(CONFIG_SND_SOC_ACPI),)\n"
        "snd-soc-acpi-y := soc-acpi.o\n"
        "endif\n"
        "obj-$(CONFIG_SND_SOC_ACPI) += snd-soc-acpi.o\n")

    hints = _module_name_hints(str(tmp_path), ["soc"])
    assert hints["soc/soc-acpi.o"] == "snd-soc-acpi"

    objs, dirs = scan_selector_scopes(str(tmp_path), ["soc"])
    # The part has no selector of its own ...
    assert exact_y_symbols_for("soc/soc-acpi.o", objs, dirs) == set()
    # ... but the composite does, and that is what governs it.
    assert exact_y_symbols_for("soc/snd-soc-acpi.o", objs, dirs) \
        == {"CONFIG_SND_SOC_ACPI"}


def test_module_name_hints_ignores_kbuild_own_list_variables(tmp_path):
    """obj-$(CONFIG_X) has the same shape as a composite module assignment."""
    from kreach.s2_presence import _module_name_hints

    (tmp_path / "snd").mkdir()
    (tmp_path / "snd" / "Makefile").write_text(
        "obj-$(CONFIG_SND_FOO) += snd-foo.o\n"
        "snd-foo-y := foo_core.o foo_pcm.o\n"
        "snd-foo-$(CONFIG_SND_FOO_DEBUG) += foo_debug.o\n"
        "lib-y += helper.o\n"
        "hostprogs-y += tool.o\n")
    hints = _module_name_hints(str(tmp_path), ["snd"])
    assert hints["snd/foo_core.o"] == "snd-foo"
    assert hints["snd/foo_pcm.o"] == "snd-foo"
    assert hints["snd/foo_debug.o"] == "snd-foo"
    assert "snd/helper.o" not in hints
    assert "snd/tool.o" not in hints


def test_read_module_map_separates_composites_from_single_file_modules(tmp_path):
    from kreach.s3_symbols import read_module_map

    d = tmp_path / "sound" / "soc"
    d.mkdir(parents=True)
    (d / "snd-soc-core.mod").write_text(
        "sound/soc/soc-core.o sound/soc/soc-ac97.o sound/soc/soc-dapm.o\n")
    (d / "snd-soc-simple.mod").write_text("sound/soc/snd-soc-simple.o\n")
    mapping, composites = read_module_map(str(tmp_path))

    assert mapping["sound/soc/soc-ac97.o"] == "snd-soc-core"
    assert composites["sound/soc/snd-soc-core.o"] == [
        "sound/soc/soc-ac97.o", "sound/soc/soc-core.o", "sound/soc/soc-dapm.o"]
    # A module built from one source is not a composite.
    assert "sound/soc/snd-soc-simple.o" not in composites


def test_presence_table_exposes_the_m_condition_the_spec_states():
    """The spec states S2's output as (condition for y, condition for m);
    the table stores (y, available) and derives m."""
    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    types = {"CONFIG_MODULES": "bool", "CONFIG_C": "tristate", "CONFIG_P": "tristate"}
    base = (F.completion_constraints(types) + F.modules_off_forces_no_m(types)
            + [F.enabled_var("CONFIG_MODULES")])

    def sat(extra):
        s = z3.Solver()
        s.add(*base, *extra)
        return s.check() == z3.sat

    # C=m: in a module, not in vmlinux.
    assert sat([F.is_m("CONFIG_C"), table.module("c.o"), z3.Not(table.builtin("c.o"))])
    # C=y: in vmlinux, so the m condition must be false.
    assert not sat([F.is_y("CONFIG_C"), table.module("c.o")])
    # C=n: neither.
    assert not sat([z3.Not(F.enabled_var("CONFIG_C")), table.module("c.o")])
    assert not sat([z3.Not(F.enabled_var("CONFIG_C")), table.builtin("c.o")])


def test_presence_table_carries_the_undefined_symbol_set():
    table = PresenceTable({}, set(), set(), {"CONFIG_GENERIC_HWEIGHT"})
    assert table.undefined == {"CONFIG_GENERIC_HWEIGHT"}
    # Default for a table built without them stays empty rather than None.
    assert PresenceTable({}, set(), set()).undefined == set()


def test_actual_levels_reads_build_root_relative_archive_members(tmp_path):
    """Kbuild's thin archives list members relative to the build root, because
    real-prereqs are prefixed with $(obj).  Reading them as archive-relative
    would double the directory."""
    import subprocess

    from kreach.acceptance import actual_levels

    b = tmp_path / "build"
    (b / "sound" / "soc").mkdir(parents=True)
    for name in ("soc-core.o", "soc-dapm.o"):
        (b / "sound" / "soc" / name).write_bytes(b"")
    # an empty file is not a valid member for real ar, so build the archive
    # with ar itself from the build root, the way Kbuild does
    subprocess.run(["ar", "cDPrST", "sound/soc/built-in.a",
                    "sound/soc/soc-core.o", "sound/soc/soc-dapm.o"],
                   cwd=str(b), check=True, stderr=subprocess.DEVNULL)
    (b / "sound" / "soc" / "snd-soc-x.mod").write_text("sound/soc/x_main.o\n")
    (b / "sound" / "soc" / "x_main.o").write_bytes(b"")

    levels = actual_levels(str(b))
    assert levels["sound/soc/soc-core.o"] == "y"
    assert levels["sound/soc/soc-dapm.o"] == "y"
    assert "sound/soc/sound/soc/soc-core.o" not in levels
    assert levels["sound/soc/x_main.o"] == "m"


def test_predicted_levels_reads_each_level_from_a_concrete_config():
    """The S2 acceptance test turns the conditions back into y/m/n for one
    .config; it has to agree with the conditions it was built from."""
    from kreach.acceptance import predicted_levels

    class FakeModel:
        kconfig_types = {"CONFIG_C": "tristate", "CONFIG_P": "tristate",
                         "CONFIG_MODULES": "bool"}

    table = _table(F.enabled_var("CONFIG_C"), F.enabled_var("CONFIG_P"))
    objs = ["c.o", "p.o"]
    m = FakeModel()

    assert predicted_levels(table, {"CONFIG_C": "y", "CONFIG_P": "m"}, m, objs) \
        == {"c.o": "y", "p.o": "m"}
    assert predicted_levels(table, {"CONFIG_C": "m"}, m, objs) \
        == {"c.o": "m", "p.o": "n"}
    assert predicted_levels(table, {}, m, objs) == {"c.o": "n", "p.o": "n"}


def test_predicted_levels_treats_an_undeclared_option_as_off():
    """CONFIG_GENERIC_HWEIGHT is absent from x86's Kconfig, so whatever a
    .config says about it, the object it guards is not built."""
    from kreach.acceptance import predicted_levels

    class FakeModel:
        kconfig_types = {"CONFIG_C": "tristate"}

    table = _table(F.enabled_var("CONFIG_C"),
                   F.enabled_var("CONFIG_GENERIC_HWEIGHT"))
    table.raw()["p.o"]["all_symbols"] = ["CONFIG_GENERIC_HWEIGHT"]
    got = predicted_levels(table, {"CONFIG_GENERIC_HWEIGHT": "y"},
                           FakeModel(), ["p.o"])
    assert got == {"p.o": "n"}


def test_s2_acceptance_separates_composite_targets_from_real_mismatches(tmp_path):
    """Kbuild links snd-pcm.o from its parts only when it is a module, so at y
    that file does not exist while its code is in vmlinux.  That is a
    definitional difference, not an S2 bug."""
    from kreach.acceptance import _composite_objects
    from kreach.config import Context
    from kreach.util import write_json

    ctx = Context(ksrc=str(tmp_path), work=str(tmp_path / "w"), arch="x86_64")
    write_json(ctx.symbols_file, {"composites": {
        "sound/core/snd-pcm.o": ["sound/core/pcm.o", "sound/core/pcm_native.o"]}})
    assert _composite_objects(ctx) == {"sound/core/snd-pcm.o"}

    # Missing or unreadable file: no composites, not a crash.
    empty = Context(ksrc=str(tmp_path), work=str(tmp_path / "w2"), arch="x86_64")
    assert _composite_objects(empty) == set()
