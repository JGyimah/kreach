# SPDX-License-Identifier: GPL-2.0-or-later
"""tools/verify_fix.py -- the part that edits real Kconfig files in place."""

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    path = os.path.join(HERE, "tools", "verify_fix.py")
    spec = importlib.util.spec_from_file_location("verify_fix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load()

KCONFIG = '''\
config SND_SOC_FSL_XCVR
\ttristate "NXP Audio Transceiver (XCVR) module support"
\tselect SND_SOC_FSL_UTILS
\thelp
\t  Say Y if you want to add support.

config SND_SOC_FSL_AUD2HTX
\ttristate "AUDIO TO HDMI TX module support"
\tdepends on ARCH_MXC || COMPILE_TEST
\tselect SND_SOC_IMX_PCM_DMA if SND_IMX_SOC != n

menuconfig SND_SOC_SOMETHING
\tbool "A bool one"
'''


def _tree(tmp_path, text=KCONFIG):
    d = tmp_path / "sound" / "soc" / "fsl"
    d.mkdir(parents=True)
    (d / "Kconfig").write_text(text)
    return str(tmp_path), "sound/soc/fsl/Kconfig"


def test_select_goes_in_after_the_type_line(tmp_path):
    ksrc, path = _tree(tmp_path)
    added = V.add_selects(ksrc, path, "CONFIG_SND_SOC_FSL_XCVR", ["BITREVERSE"])
    assert added == ["BITREVERSE"]
    lines = open(os.path.join(ksrc, path)).read().splitlines()
    assert lines[0] == "config SND_SOC_FSL_XCVR"
    assert lines[1].strip().startswith("tristate")
    assert lines[2] == "\tselect BITREVERSE"
    # The neighbouring stanza is untouched.
    assert "\tselect BITREVERSE\n" not in "\n".join(lines[5:])


def test_a_select_already_present_is_left_alone(tmp_path):
    ksrc, path = _tree(tmp_path)
    before = open(os.path.join(ksrc, path)).read()
    assert V.add_selects(ksrc, path, "CONFIG_SND_SOC_FSL_XCVR",
                         ["SND_SOC_FSL_UTILS"]) == []
    assert open(os.path.join(ksrc, path)).read() == before


def test_two_selects_at_once(tmp_path):
    ksrc, path = _tree(tmp_path)
    added = V.add_selects(ksrc, path, "CONFIG_SND_SOC_FSL_AUD2HTX",
                          ["REGMAP_MMIO", "SND_SOC_GENERIC_DMAENGINE_PCM"])
    assert added == ["REGMAP_MMIO", "SND_SOC_GENERIC_DMAENGINE_PCM"]
    text = open(os.path.join(ksrc, path)).read()
    assert "\tselect REGMAP_MMIO\n" in text
    assert "\tselect SND_SOC_GENERIC_DMAENGINE_PCM\n" in text
    # Inserted into its own stanza, not the one above.
    xcvr = text.split("config SND_SOC_FSL_AUD2HTX")[0]
    assert "REGMAP_MMIO" not in xcvr


def test_menuconfig_and_bool_stanzas_are_found_too(tmp_path):
    ksrc, path = _tree(tmp_path)
    assert V.add_selects(ksrc, path, "CONFIG_SND_SOC_SOMETHING", ["FOO"]) == ["FOO"]
    assert "\tselect FOO\n" in open(os.path.join(ksrc, path)).read()


def test_a_missing_stanza_is_an_error_not_a_silent_no_op(tmp_path):
    ksrc, path = _tree(tmp_path)
    with pytest.raises(SystemExit):
        V.add_selects(ksrc, path, "CONFIG_NOT_THERE", ["FOO"])


def test_kconfig_warnings_picks_out_the_select_complaints():
    log = ("  HOSTCC  scripts/kconfig/conf.o\n"
           "drivers/foo/Kconfig:12:warning: unmet direct dependencies "
           "detected for BAR\n"
           "#\n# configuration written to .config\n#\n")
    assert V.kconfig_warnings(log) == [
        "drivers/foo/Kconfig:12:warning: unmet direct dependencies "
        "detected for BAR"]
    assert V.kconfig_warnings("  CC  init/main.o\n") == []


def test_the_select_table_covers_every_symbol_the_findings_name():
    """If a finding's symbol is not in the table the tool skips it rather
    than guessing, so the table and the published findings must agree."""
    import json
    path = os.path.join(HERE, "results", "v7.3", "findings.jsonl")
    if not os.path.exists(path):
        pytest.skip("no published v7.3 findings")
    named = set()
    for line in open(path):
        if line.strip():
            rec = json.loads(line)
            named.update(rec["symbols"])
    assert named <= set(V.SELECT_FOR), named - set(V.SELECT_FOR)
