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


# --- mining: the other two ways a pair cannot be a y/m instance -----------

class _FakeGit:
    """Stands in for _git over a tiny tree of Kconfig files."""

    def __init__(self, files):
        self.files = files            # {path: text}

    def __call__(self, ctx, args, check=True):
        if args[0] == "grep":
            name = args[2].split()[-1].rstrip("$")
            out = []
            for path, text in self.files.items():
                for l in text.splitlines():
                    if l.strip() in ("config %s" % name, "menuconfig %s" % name):
                        out.append("%s:%s" % (args[3], path))
                        break
            return "\n".join(out)
        if args[0] == "show":
            return self.files.get(args[1].split(":", 1)[1], "")
        return ""


TREE = {
    "drivers/net/ethernet/freescale/Kconfig":
        "config NET_VENDOR_FREESCALE\n\tbool \"Freescale devices\"\n\tdefault y\n",
    "drivers/net/macsec/Kconfig":
        "config MACSEC\n\ttristate \"IEEE 802.1AE MACsec support\"\n",
    "arch/powerpc/platforms/pseries/Kconfig":
        "config PPC_PSERIES\n\tbool \"IBM pSeries\"\n",
    "arch/arm64/Kconfig.platforms":
        "config ARCH_MESON\n\tbool \"Amlogic\"\n",
    "init/Kconfig":
        "config COMPILE_TEST\n\tbool \"Compile also drivers which will not load\"\n",
    "drivers/base/Kconfig":
        "config SCSI\n\ttristate \"SCSI support\"\n",
}


def _patched(monkeypatch):
    from kreach import benchmark as B
    monkeypatch.setattr(B, "_git", _FakeGit(TREE))
    return B


def test_a_bool_provider_cannot_hold_the_m_the_case_needs(monkeypatch):
    """NET_VENDOR_FREESCALE is a menu gate: a bool has no m, so no
    configuration satisfies level(provider) == m."""
    B = _patched(monkeypatch)
    assert not B.provider_can_be_a_module(None, "rev", "CONFIG_NET_VENDOR_FREESCALE", {})
    assert B.provider_can_be_a_module(None, "rev", "CONFIG_MACSEC", {})


def test_an_undeclared_provider_is_left_alone(monkeypatch):
    """Not finding a stanza is not evidence; do not guess."""
    B = _patched(monkeypatch)
    assert B.provider_can_be_a_module(None, "rev", "CONFIG_NOWHERE", {})


def test_a_caller_gated_on_another_architecture_is_unbuildable(monkeypatch):
    """PPC_PSERIES is declared only under arch/powerpc, so an x86_64 run can
    never compile the caller and the reference is never seen."""
    B = _patched(monkeypatch)
    assert not B.caller_buildable_on(None, "rev", "x86", "CONFIG_SCSI_IBMVFC",
                                     ["PPC_PSERIES && SCSI"], {})
    assert not B.caller_buildable_on(None, "rev", "x86", "CONFIG_AMLOGIC_THERMAL",
                                     ["OF && ARCH_MESON"], {})


def test_compile_test_makes_it_buildable_anywhere(monkeypatch):
    """NET_DSA_NETC_SWITCH's shape: gated on ARM64 but with the escape."""
    B = _patched(monkeypatch)
    assert B.caller_buildable_on(None, "rev", "x86", "CONFIG_NET_DSA_NETC_SWITCH",
                                 ["ARM64 || COMPILE_TEST", "NET_DSA && PCI"], {})


def test_a_dependency_on_this_architecture_is_fine(monkeypatch):
    B = _patched(monkeypatch)
    assert B.caller_buildable_on(None, "rev", "arm64", "CONFIG_AMLOGIC_THERMAL",
                                 ["OF && ARCH_MESON"], {})
    # A plain tristate dependency says nothing about architecture.
    assert B.caller_buildable_on(None, "rev", "x86", "CONFIG_X", ["SCSI"], {})


# --- scoring: when the two sides name the caller differently -------------

def _finding(caller_opt, provider_opt, symbol, source):
    return {"caller": {"option": caller_opt, "source": source},
            "provider": {"option": provider_opt}, "symbols": [symbol]}


def _write(tmp_path, findings):
    import json
    p = tmp_path / "findings.jsonl"
    p.write_text("".join(json.dumps(f) + "\n" for f in findings))
    return str(p)


MLX5 = {
    "commit": "87ab8276ed2400000000000000000000000000aa",
    "subject": "eth: mlx5: fix macsec dependency", "date": "2026-01-01",
    "parent": "47b6bcef6e67", "files": ["drivers/net/ethernet/.../Kconfig"],
    "caller_option": "CONFIG_MLX5_MACSEC",
    "provider_options": ["CONFIG_MACSEC", "CONFIG_MLX5_CORE"],
    "symbols": ["macsec_netdev_is_offloaded"], "fix_patterns": [], "matched": True,
}


class _Grep:
    """_git stub: the symbol is referenced by the infiniband caller."""

    def __init__(self, paths):
        self.paths = paths

    def __call__(self, ctx, args, check=True):
        if args[0] == "grep":
            return "\n".join("%s:%s" % (args[-1], p) for p in self.paths)
        return ""


def test_exact_pair_is_still_the_preferred_hit(tmp_path):
    from kreach.benchmark import score

    f = _finding("CONFIG_MLX5_MACSEC", "CONFIG_MACSEC",
                 "macsec_netdev_is_offloaded", "drivers/x.c")
    res = score({"cases": [MLX5]}, _write(tmp_path, [f]), commit=MLX5["commit"])
    assert res["recall"] == 1.0
    assert res["hits"][0]["matched"] == "pair"


def test_a_differently_named_caller_counts_when_the_symbol_matches(tmp_path, monkeypatch):
    """The real case: the fix edits the bool MLX5_MACSEC, kreach names the
    tristate MLX5_INFINIBAND whose level puts the object in vmlinux."""
    from kreach import benchmark as B

    src = "drivers/infiniband/hw/mlx5/macsec.c"
    monkeypatch.setattr(B, "_git", _Grep([src, "drivers/net/macsec.c"]))
    f = _finding("CONFIG_MLX5_INFINIBAND", "CONFIG_MACSEC",
                 "macsec_netdev_is_offloaded", src)
    res = B.score({"cases": [MLX5]}, _write(tmp_path, [f]),
                  commit=MLX5["commit"], ctx=object())
    assert res["recall"] == 1.0
    assert res["hits"][0]["matched"] == "symbol and provider"
    assert "MLX5_INFINIBAND" in res["hits"][0]["note"]


def test_the_caller_must_be_a_file_that_references_the_symbol(tmp_path, monkeypatch):
    """Otherwise a different driver's bug on the same symbol would be
    credited as finding this one."""
    from kreach import benchmark as B

    monkeypatch.setattr(B, "_git", _Grep(["drivers/infiniband/hw/mlx5/macsec.c"]))
    f = _finding("CONFIG_AQTION", "CONFIG_MACSEC", "macsec_netdev_is_offloaded",
                 "drivers/net/ethernet/aquantia/atlantic/aq_macsec.c")
    res = B.score({"cases": [MLX5]}, _write(tmp_path, [f]),
                  commit=MLX5["commit"], ctx=object())
    assert res["recall"] == 0.0
    assert res["misses"][0]["reason"] == "not reported"


def test_a_different_symbol_is_not_credited(tmp_path, monkeypatch):
    from kreach import benchmark as B

    src = "drivers/infiniband/hw/mlx5/macsec.c"
    monkeypatch.setattr(B, "_git", _Grep([src]))
    f = _finding("CONFIG_MLX5_INFINIBAND", "CONFIG_MACSEC", "something_else", src)
    res = B.score({"cases": [MLX5]}, _write(tmp_path, [f]),
                  commit=MLX5["commit"], ctx=object())
    assert res["recall"] == 0.0


def test_without_a_tree_only_exact_pairs_count(tmp_path):
    """The source check is what keeps it tight; with no ctx there is nothing
    to check it against, so do not guess."""
    from kreach.benchmark import score

    f = _finding("CONFIG_MLX5_INFINIBAND", "CONFIG_MACSEC",
                 "macsec_netdev_is_offloaded",
                 "drivers/infiniband/hw/mlx5/macsec.c")
    res = score({"cases": [MLX5]}, _write(tmp_path, [f]), commit=MLX5["commit"])
    assert res["recall"] == 0.0


def test_symbol_sources_matches_whole_words_only(monkeypatch):
    """A substring match would pull in every file mentioning a longer
    identifier, loosening the source check the near-match rule depends on."""
    from kreach import benchmark as B

    seen = {}

    def fake_git(ctx, args, check=True):
        seen["args"] = args
        return "rev:drivers/x/a.c"

    monkeypatch.setattr(B, "_git", fake_git)
    out = B.symbol_sources(None, "rev", ["foo"], {})
    assert out == {"drivers/x/a.c"}
    assert "-lw" in seen["args"], seen["args"]
