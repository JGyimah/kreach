# SPDX-License-Identifier: GPL-2.0-or-later
"""Benchmark mining: extract the caller/provider pair a real Kconfig fix
addressed, and score a findings file against it."""

import kreach.benchmark as B
from kreach.util import write_jsonl

COMMIT = """\
ALSA: foo: fix build when BAR is a module

When CONFIG_SND_FOO=y and CONFIG_SND_BAR=m the build fails:

  ld: sound/soc/foo.o: undefined reference to `bar_register'

Add the missing dependency.

Fixes: 1234567890ab ("ALSA: foo: use the bar helper")
Signed-off-by: Someone <someone@example.com>

diff --git a/sound/soc/Kconfig b/sound/soc/Kconfig
--- a/sound/soc/Kconfig
+++ b/sound/soc/Kconfig
@@ -10,6 +10,7 @@ config SND_FOO
 	tristate "Foo codec"
 	depends on I2C
+	depends on SND_BAR || !SND_BAR
 	help
 	  Foo.
"""

REACHABLE_COMMIT = """\
media: baz: guard the call

  undefined reference to `qux_init'

diff --git a/drivers/media/baz.c b/drivers/media/baz.c
--- a/drivers/media/baz.c
+++ b/drivers/media/baz.c
@@ -1,3 +1,3 @@
-	qux_init();
+	if (IS_REACHABLE(CONFIG_QUX))
+		qux_init();
"""


def test_extract_pair_reads_symbol_caller_and_provider(monkeypatch):
    monkeypatch.setattr(B, "_git", lambda ctx, args, check=True: COMMIT)
    rec = B.extract_pair(None, {"commit": "abc", "subject": "s", "date": "2026-01-01"})
    assert rec["symbols"] == ["bar_register"]
    assert rec["caller_option"] == "CONFIG_SND_FOO"
    assert "CONFIG_SND_BAR" in rec["provider_options"]
    assert "depends on X || !X" in rec["fix_patterns"]


def test_extract_pair_recognises_an_is_reachable_fix(monkeypatch):
    monkeypatch.setattr(B, "_git", lambda ctx, args, check=True: REACHABLE_COMMIT)
    rec = B.extract_pair(None, {"commit": "def", "subject": "s", "date": "2026-01-01"})
    assert rec["symbols"] == ["qux_init"]
    assert "CONFIG_QUX" in rec["provider_options"]
    assert "IS_REACHABLE" in rec["fix_patterns"]


def test_kconfig_file_filter_accepts_only_kconfig_paths():
    good = ["sound/soc/Kconfig", "drivers/gpu/drm/Kconfig.debug", "Kconfig"]
    bad = ["sound/soc/foo.c", "Makefile", "include/linux/bar.h", "KconfigEditor.py"]
    assert all(B.KCONFIG_FILE_RE.search(f) for f in good)
    assert not any(B.KCONFIG_FILE_RE.search(f) for f in bad)


def finding(caller, provider):
    return {"id": caller + provider, "case": "y/m",
            "caller": {"option": caller}, "provider": {"option": provider},
            "symbols": ["bar_register"], "kernel": "v6.17"}


def test_score_counts_a_hit_when_the_same_pair_is_reported(tmp_path):
    path = str(tmp_path / "f.jsonl")
    write_jsonl(path, [finding("CONFIG_SND_FOO", "CONFIG_SND_BAR")])
    bench = {"cases": [{"commit": "abc", "subject": "s",
                        "caller_option": "CONFIG_SND_FOO",
                        "provider_options": ["CONFIG_SND_BAR"]}]}
    got = B.score(bench, path)
    assert got["recall"] == 1.0
    assert got["hits"][0]["commit"] == "abc"


def test_score_distinguishes_wrong_provider_from_not_reported(tmp_path):
    path = str(tmp_path / "f.jsonl")
    write_jsonl(path, [finding("CONFIG_SND_FOO", "CONFIG_OTHER")])
    bench = {"cases": [
        {"commit": "abc", "subject": "same caller",
         "caller_option": "CONFIG_SND_FOO", "provider_options": ["CONFIG_SND_BAR"]},
        {"commit": "xyz", "subject": "absent",
         "caller_option": "CONFIG_NOPE", "provider_options": ["CONFIG_SND_BAR"]},
    ]}
    got = B.score(bench, path)
    assert got["recall"] == 0.0
    reasons = {m["commit"]: m["reason"] for m in got["misses"]}
    assert reasons["abc"] == "caller reported, different provider"
    assert reasons["xyz"] == "not reported"


def test_score_skips_cases_with_no_extractable_pair(tmp_path):
    path = str(tmp_path / "f.jsonl")
    write_jsonl(path, [])
    bench = {"cases": [{"commit": "abc", "subject": "s",
                        "caller_option": None, "provider_options": []}]}
    assert B.score(bench, path)["checked"] == 0


# --- scoring: the commit filter ------------------------------------------

def test_same_commit_accepts_either_side_abbreviated():
    """The benchmark stores 40 characters; people type twelve."""
    from kreach.benchmark import same_commit

    full = "999fb9d51f939ee23cbb9313ae558d29d6987804"
    assert same_commit(full, "999fb9d51f93")
    assert same_commit("999fb9d51f93", full)
    assert same_commit(full, full)
    assert not same_commit(full, "deadbeef1234")
    # Too short to mean anything: six hex digits would match by accident.
    assert not same_commit(full, "999fb9")
    assert not same_commit(full, None)


def _case(commit, caller="CONFIG_A", providers=("CONFIG_P",)):
    return {"commit": commit, "subject": "s", "date": "2026-01-01",
            "caller_option": caller, "provider_options": list(providers),
            "symbols": ["sym"], "files": ["sound/x/Kconfig"], "parent": "p",
            "fix_patterns": ["depends on X || !X"], "matched": True}


def _findings_file(tmp_path, caller="CONFIG_A", provider="CONFIG_P"):
    import json
    p = tmp_path / "findings.jsonl"
    p.write_text(json.dumps({
        "caller": {"option": caller}, "provider": {"option": provider}}) + "\n")
    return str(p)


def test_score_hits_on_an_abbreviated_commit(tmp_path):
    """The regression that made the benchmark unrunnable: a short SHA scored
    zero cases and a null recall, which reads like a miss and is not one."""
    from kreach.benchmark import score

    full = "999fb9d51f939ee23cbb9313ae558d29d6987804"
    res = score({"cases": [_case(full)]}, _findings_file(tmp_path),
                commit="999fb9d51f93")
    assert res["checked"] == 1
    assert res["recall"] == 1.0
    assert tuple(res["hits"][0]["pair"]) == ("CONFIG_A", "CONFIG_P")


def test_score_separates_wrong_provider_from_not_reported(tmp_path):
    from kreach.benchmark import score

    full = "999fb9d51f939ee23cbb9313ae558d29d6987804"
    res = score({"cases": [_case(full)]},
                _findings_file(tmp_path, provider="CONFIG_OTHER"),
                commit=full)
    assert res["checked"] == 1 and res["recall"] == 0.0
    assert res["misses"][0]["reason"] == "caller reported, different provider"

    res = score({"cases": [_case(full)]},
                _findings_file(tmp_path, caller="CONFIG_ELSEWHERE"), commit=full)
    assert res["misses"][0]["reason"] == "not reported"


# --- mining: pairs that cannot be a y/m instance --------------------------

def test_and_terms_splits_at_the_top_level_only():
    from kreach.benchmark import and_terms

    assert and_terms("SPI_MASTER && I2C") == ["SPI_MASTER", "I2C"]
    assert and_terms("A && (B || C)") == ["A", "(B || C)"]
    assert and_terms("X") == ["X"]
    assert and_terms("(A && B) || C") == ["(A && B) || C"]


def test_a_tristate_depending_on_the_provider_cannot_be_the_ym_case():
    """The rt5575 shape: 'depends on SPI_MASTER' caps the caller at m, so
    caller=y with SPI_MASTER=m is not reachable and the extracted pair is not
    the thing that broke."""
    from kreach.benchmark import dependency_caps_level

    deps = ["SPI_MASTER && I2C", "SND_SOC_RT5575"]
    assert dependency_caps_level("tristate", deps, "CONFIG_SPI_MASTER")
    assert dependency_caps_level("tristate", deps, "CONFIG_SND_SOC_RT5575")
    assert not dependency_caps_level("tristate", deps, "CONFIG_OF")


def test_a_bool_caller_is_the_hazard_not_the_protection():
    """A bool has no m level, so 'depends on P' leaves it y with P at m.
    That pair must be kept -- it is exactly what kreach looks for."""
    from kreach.benchmark import dependency_caps_level

    assert not dependency_caps_level("bool", ["SPI_MASTER"], "CONFIG_SPI_MASTER")
    assert not dependency_caps_level(None, ["SPI_MASTER"], "CONFIG_SPI_MASTER")


def test_an_exact_level_test_is_not_a_cap():
    """'depends on P=y' is the fix for this class, not an instance of it."""
    from kreach.benchmark import dependency_caps_level

    assert not dependency_caps_level("tristate", ["SPI_MASTER=y"], "CONFIG_SPI_MASTER")
    assert not dependency_caps_level("tristate", ["A || SPI_MASTER"], "CONFIG_SPI_MASTER")


def test_kconfig_stanza_reads_type_and_depends(tmp_path, monkeypatch):
    from kreach import benchmark as B

    text = """config SND_SOC_RT5575
\ttristate "Realtek ALC5575 Codec - I2C"
\tdepends on I2C

config SND_SOC_RT5575_SPI
\ttristate "Realtek ALC5575 Codec - SPI"
\tdepends on SPI_MASTER && I2C
\tdepends on SND_SOC_RT5575

config SOMETHING_ELSE
\tbool
"""
    monkeypatch.setattr(B, "_git", lambda ctx, args, check=True: text)
    typ, deps = B.kconfig_stanza(None, "parent", ["sound/soc/codecs/Kconfig"],
                                 "CONFIG_SND_SOC_RT5575_SPI")
    assert typ == "tristate"
    assert deps == ["SPI_MASTER && I2C", "SND_SOC_RT5575"]

    # The stanza must stop at the next one, not run into it.
    typ, deps = B.kconfig_stanza(None, "parent", ["sound/soc/codecs/Kconfig"],
                                 "CONFIG_SND_SOC_RT5575")
    assert (typ, deps) == ("tristate", ["I2C"])


def test_score_does_not_count_an_excluded_case_as_a_miss(tmp_path):
    from kreach.benchmark import score

    full = "999fb9d51f939ee23cbb9313ae558d29d6987804"
    case = _case(full)
    case["excluded"] = "caller depends on provider"
    res = score({"cases": [case]}, _findings_file(tmp_path, caller="CONFIG_OTHER"),
                commit=full)
    assert res["checked"] == 0
    assert res["misses"] == []
    assert res["excluded"][0]["commit"] == full
