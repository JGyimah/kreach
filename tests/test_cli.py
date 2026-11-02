# SPDX-License-Identifier: GPL-2.0-or-later
"""`kreach run` calls the stage commands with its own argument namespace, so
that namespace has to carry every option they read.  argparse will not catch a
missing one -- it surfaces as AttributeError half way through a long run."""

import pytest

from kreach.cli import build_parser


STAGE_ARGS = {
    "s1": ["force", "dimacs", "no_repair", "r5"],
    "s2": ["force", "subdir"],
    "s3": ["config", "whole_tree", "skip_build", "force", "subdir"],
    "s4": ["max_candidates", "use_feedback"],
    "s5": ["case", "limit", "no_witness", "caller", "provider"],
    "s6": ["limit", "timeout", "parallel", "build_jobs", "keep_build",
           "ccache", "rebuild", "provider", "caller",
           "no_reference_check"],
    "s7": ["quiet"],
}

COMMON = ["ksrc", "work", "arch", "jobs", "verbose", "log"]


def test_every_subcommand_parses():
    p = build_parser()
    for cmd in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "run", "status"):
        args = p.parse_args([cmd])
        assert callable(args.func)


@pytest.mark.parametrize("stage,options", sorted(STAGE_ARGS.items()))
def test_run_namespace_covers_every_stage_option(stage, options):
    run_args = build_parser().parse_args(["run"])
    missing = [o for o in options if not hasattr(run_args, o)]
    assert not missing, "kreach run is missing %s needed by %s" % (missing, stage)


def test_common_options_are_present_on_every_subcommand():
    p = build_parser()
    for cmd in sorted(STAGE_ARGS) + ["run", "status", "compare", "bench",
                                     "score", "randcompare"]:
        argv = [cmd] + (["x"] if cmd == "compare" else [])
        args = p.parse_args(argv)
        for opt in COMMON:
            assert hasattr(args, opt), "%s lacks %s" % (cmd, opt)


def test_test_subcommand_requires_a_known_stage():
    p = build_parser()
    assert p.parse_args(["test", "s1"]).stage == "s1"
    with pytest.raises(SystemExit):
        p.parse_args(["test", "s9"])


def test_s5_rejects_an_unknown_case():
    p = build_parser()
    assert p.parse_args(["s5", "--case", "y/m"]).case == ["y/m"]
    with pytest.raises(SystemExit):
        p.parse_args(["s5", "--case", "y/y"])


def test_subdir_is_repeatable():
    args = build_parser().parse_args(["s2", "-d", "sound", "-d", "drivers"])
    assert args.subdir == ["sound", "drivers"]


def test_work_lock_keeps_two_runs_out_of_one_work_directory(tmp_path):
    """Two S6 runs sharing a work directory delete each other's build trees,
    which surfaces as a compile error and gets recorded as a verdict on a
    finding.  The lock turns that into a clear refusal."""
    import pytest as _pytest

    from kreach.util import WorkDirLocked, work_lock

    work = str(tmp_path)
    with work_lock(work, "s6"):
        with _pytest.raises(WorkDirLocked) as caught:
            with work_lock(work, "s6"):
                pass
        assert "already running" in str(caught.value)
        # A different stage is not blocked.
        with work_lock(work, "s5"):
            pass
    # Released on exit, so the next run proceeds.
    with work_lock(work, "s6"):
        pass


def test_work_lock_is_per_work_directory(tmp_path):
    from kreach.util import work_lock

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with work_lock(str(a), "s6"), work_lock(str(b), "s6"):
        pass


def test_meta_identity_survives_a_later_stage(tmp_path, monkeypatch):
    """A later stage must not relabel a finished run with whatever the tree
    is checked out at now.  Caught by publishing a v6.17 result set while the
    tree sat at v7.3: every field still looked right and the kernel was
    wrong."""
    from kreach.config import Context
    from kreach.util import read_json

    ctx = Context(ksrc=str(tmp_path / "src"), work=str(tmp_path / "w"), arch="x86_64")
    monkeypatch.setattr(Context, "kernel_commit", lambda self: "aaaaaaaaaaaa")
    monkeypatch.setattr(Context, "kernel_describe", lambda self: "v6.17")
    monkeypatch.setattr(Context, "kernel_version", lambda self: "6.17.0")
    ctx.write_meta(stamp=True)                      # S1, at v6.17

    # The tree moves on, and a later stage records its own statistics.
    monkeypatch.setattr(Context, "kernel_commit", lambda self: "bbbbbbbbbbbb")
    monkeypatch.setattr(Context, "kernel_describe", lambda self: "v7.3")
    monkeypatch.setattr(Context, "kernel_version", lambda self: "7.3.0")
    ctx.write_meta({"s7": {"findings": 1}})

    meta = read_json(str(tmp_path / "w" / "meta.json"))
    assert meta["kernel_describe"] == "v6.17"
    assert meta["kernel_commit"] == "aaaaaaaaaaaa"
    assert meta["s7"] == {"findings": 1}

    # Re-running S1 is how you move a work directory to another revision.
    ctx.write_meta(stamp=True)
    assert read_json(str(tmp_path / "w" / "meta.json"))["kernel_describe"] == "v7.3"


def test_meta_fills_identity_when_it_is_missing(tmp_path, monkeypatch):
    """A work directory from before this behaviour still gets stamped."""
    from kreach.config import Context
    from kreach.util import read_json

    ctx = Context(ksrc=str(tmp_path / "src"), work=str(tmp_path / "w"), arch="x86_64")
    monkeypatch.setattr(Context, "kernel_commit", lambda self: "cccccccccccc")
    monkeypatch.setattr(Context, "kernel_describe", lambda self: "v7.3")
    monkeypatch.setattr(Context, "kernel_version", lambda self: "7.3.0")
    ctx.write_meta({"s2": {"objects": 3}})          # no stamp, nothing on disk
    assert read_json(str(tmp_path / "w" / "meta.json"))["kernel_describe"] == "v7.3"
