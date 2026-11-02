# SPDX-License-Identifier: GPL-2.0-or-later
"""kreach command line.

Each stage is its own subcommand so it can be rerun, cached and tested on its
own; ``kreach run`` chains S1..S7.
"""

import argparse
import json
import os
import sys

from kreach import __version__
from kreach.config import DEFAULT_ARCH, WIDE_CONFIGS, Context
from kreach.util import (WorkDirLocked, ensure_tool_path, get_logger,
                         read_json, setup_logging, work_lock, write_json)

log = get_logger("cli")

# The main code directories, plus the architecture's own: providers live
# there too, and missing them makes S4 pick a same-named object elsewhere in
# the tree (lib/hweight.o instead of arch/x86/lib/hweight.o).
DEFAULT_SUBDIRS = ["drivers/", "sound/", "net/", "fs/", "kernel/", "lib/",
                   "crypto/", "mm/", "block/", "security/", "virt/", "init/",
                   "ipc/"]


def _default_subdirs(ctx) -> list[str]:
    return DEFAULT_SUBDIRS + ["arch/%s/" % ctx.srcarch]


def _ctx(args) -> Context:
    return Context(ksrc=args.ksrc, work=args.work, arch=args.arch, jobs=args.jobs)


def _subdirs(args, ctx=None) -> list[str]:
    if args.subdir:
        return [d if d.endswith("/") else d + "/" for d in args.subdir]
    return _default_subdirs(ctx or _ctx(args))


# ---------------------------------------------------------------- stages ----

def cmd_s1(args):
    from kreach.s1_kconfig import dump_dimacs, run_s1
    ctx = _ctx(args)
    # S1 is the stage that fixes which tree this work directory is about.
    ctx.write_meta(stamp=True)
    model = run_s1(ctx, force=args.force, repair=not args.no_repair,
                   r5=getattr(args, "r5", False))
    if args.dimacs:
        dump_dimacs(ctx, model)
    return 0


def cmd_s2(args):
    from kreach.s1_kconfig import load_s1
    from kreach.s2_presence import run_s2
    ctx = _ctx(args)
    model = load_s1(ctx)
    run_s2(ctx, _subdirs(args, ctx), model.kconfig_types, force=args.force)
    return 0


def cmd_s3(args):
    from kreach.s2_presence import apply_module_map, load_s2
    from kreach.s3_symbols import run_s3
    ctx = _ctx(args)
    blob = run_s3(ctx, configs=args.config or list(WIDE_CONFIGS),
                  subdirs=(_subdirs(args) if not args.whole_tree else None),
                  force=args.force, skip_build=args.skip_build)
    try:
        apply_module_map(ctx, load_s2(ctx), blob.get("module_map", {}))
    except FileNotFoundError:
        log.info("no S2 table yet; module names will be filled in later")
    return 0


def cmd_s4(args):
    from kreach.s1_kconfig import load_s1
    from kreach.s2_presence import load_s2
    from kreach.s3_symbols import load_s3
    from kreach.s4_refcond import feedback_from_s6, run_s4
    ctx = _ctx(args)
    model, table, symbols = load_s1(ctx), load_s2(ctx), load_s3(ctx)
    feedback = feedback_from_s6(ctx) if args.use_feedback else None
    if feedback:
        log.info("S6 feedback: skipping %d previously rejected pairs",
                 len(feedback["rejected_pairs"]))
    run_s4(ctx, model, table, symbols, max_candidates=args.max_candidates,
           feedback=feedback)
    return 0


def cmd_s5(args):
    from kreach.s1_kconfig import load_s1
    from kreach.s2_presence import load_s2
    from kreach.s4_refcond import load_candidates
    from kreach.s5_solve import run_s5
    ctx = _ctx(args)
    model, table = load_s1(ctx), load_s2(ctx)
    from kreach.s5_solve import filter_candidates

    cands = filter_candidates(load_candidates(ctx), callers=args.caller,
                              providers=args.provider)
    cases = args.case or ["y/m"]
    log.info("S5: %d candidates, cases %s", len(cands), ",".join(cases))
    run_s5(ctx, model, table, cands, cases=cases, limit=args.limit,
           skip_materialise=args.no_witness)
    return 0


def cmd_s6(args):
    from kreach.s1_kconfig import load_s1
    from kreach.s2_presence import load_s2
    ctx = _ctx(args)
    # The model and the presence table let S6 re-check each witness before
    # spending a build on it.
    try:
        model, table = load_s1(ctx), load_s2(ctx)
    except FileNotFoundError as exc:
        log.warning("%s -- witnesses will be built without a pre-check", exc)
        model = table = None
    with work_lock(ctx.work, "s6"):
        return _run_s6(ctx, args, model, table)


def _run_s6(ctx, args, model, table):
    from kreach.s5_solve import load_queries
    from kreach.s6_confirm import run_s6

    run_s6(ctx, load_queries(ctx), limit=args.limit, timeout=args.timeout,
           parallel=args.parallel, jobs=args.build_jobs,
           keep_build=args.keep_build, ccache=args.ccache, rebuild=args.rebuild,
           model=model, table=table, providers=args.provider,
           callers=args.caller, check_reference=not args.no_reference_check)
    return 0


def cmd_s7(args):
    from kreach.s6_confirm import load_results
    from kreach.s7_report import publish, run_s7
    ctx = _ctx(args)
    findings = run_s7(ctx, load_results(ctx))
    if findings and getattr(args, "publish", None):
        publish(ctx, findings, args.publish)
    if findings and not args.quiet:
        print(open(ctx.report_file).read())
    return 0


def cmd_run(args):
    """S1..S7 in order."""
    rc = cmd_s1(args)
    rc = rc or cmd_s2(args)
    rc = rc or cmd_s3(args)
    rc = rc or cmd_s4(args)
    rc = rc or cmd_s5(args)
    rc = rc or cmd_s6(args)
    rc = rc or cmd_s7(args)
    return rc


# ------------------------------------------------------------- utilities ----

def cmd_status(args):
    ctx = _ctx(args)
    meta = ctx.read_meta()
    print(json.dumps(meta, indent=2, sort_keys=True))
    for name, path in [
        ("s1 model", ctx.model_file), ("s2 presence", ctx.presence_file),
        ("s3 symbols", ctx.symbols_file), ("s4 candidates", ctx.candidates_file),
        ("s5 queries", ctx.queries_file), ("s6 results", ctx.confirm_file),
        ("s7 findings", ctx.findings_file),
    ]:
        state = "%9d bytes" % os.path.getsize(path) if os.path.exists(path) else "       missing"
        print("%-14s %s  %s" % (name, state, path))
    return 0


def cmd_compare(args):
    from kreach.s7_report import compare
    ctx = _ctx(args)
    result = compare(args.previous, args.current or ctx.findings_file)
    print(json.dumps(result, indent=2))
    if args.output:
        write_json(args.output, result)
    return 0


def cmd_randcompare(args):
    """How many randconfigs would it take to hit each finding by chance?"""
    from kreach import randcompare
    from kreach.s1_kconfig import load_s1
    from kreach.s2_presence import load_s2
    ctx = _ctx(args)
    model, table = load_s1(ctx), load_s2(ctx)
    findings = randcompare.load_findings(ctx)
    res = randcompare.run(ctx, model, table, findings,
                          samples=args.samples, seed=args.seed)
    print(json.dumps(res, indent=2))
    out = args.output or os.path.join(ctx.work, "randcompare.json")
    write_json(out, res)
    log.info("wrote %s", out)
    return 0


def cmd_test(args):
    from kreach import acceptance
    ctx = _ctx(args)
    if args.stage == "s1":
        res = acceptance.test_s1(ctx, samples=args.samples, seed=args.seed)
    elif args.stage == "s2":
        res = acceptance.test_s2(ctx, configs=args.config, randconfigs=args.samples,
                                 subdirs=_subdirs(args, ctx),
                                 reuse_s3_build=args.reuse_s3_build)
    else:
        raise SystemExit("unknown stage %r" % args.stage)
    print(json.dumps(res, indent=2, sort_keys=True))
    out = os.path.join(ctx.work, "acceptance-%s.json" % args.stage)
    write_json(out, res)
    log.info("wrote %s", out)
    return 0 if res.get("pass") else 1


def cmd_bench(args):
    from kreach import benchmark
    ctx = _ctx(args)
    res = benchmark.build_benchmark(ctx, limit=args.limit, since=args.since)
    out = args.output or os.path.join(ctx.work, "benchmark.json")
    write_json(out, res)
    log.info("wrote %s (%d cases, %d usable)", out, len(res["cases"]), res["usable"])
    if not args.quiet:
        print(json.dumps(res, indent=2))
    return 0


def cmd_score(args):
    """Score a findings file against a mined benchmark (historical recall)."""
    from kreach import benchmark
    ctx = _ctx(args)
    bench = read_json(args.benchmark or os.path.join(ctx.work, "benchmark.json"))
    findings = args.findings or ctx.findings_file
    if not os.path.exists(findings):
        raise FileNotFoundError("no findings at %s -- run 'kreach s7' first" % findings)
    res = benchmark.score(bench, findings, commit=args.commit)
    print(json.dumps(res, indent=2))
    if args.output:
        write_json(args.output, res)
    log.info("historical recall: %s of %d benchmark cases",
             res["recall"], res["checked"])
    return 0


# ------------------------------------------------------------------ main ----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kreach",
        description="Kconfig Reachability Checker: find configurations that "
                    "Kconfig accepts but that fail to link.")
    p.add_argument("--version", action="version", version="kreach %s" % __version__)
    p.add_argument("-k", "--ksrc", default=os.environ.get("KREACH_KSRC", "."),
                   help="kernel source tree (default $KREACH_KSRC or .)")
    p.add_argument("-w", "--work", default=os.environ.get("KREACH_WORK", "kreach-out"),
                   help="work directory for stage outputs")
    p.add_argument("-a", "--arch", default=DEFAULT_ARCH, help="target ARCH")
    p.add_argument("-j", "--jobs", type=int, default=None, help="make parallelism")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log", help="also write the log to this file")

    sub = p.add_subparsers(dest="cmd", required=True)

    def add_subdir(sp):
        sp.add_argument("-d", "--subdir", action="append",
                        help="restrict to this subtree (repeatable); default is "
                             "the main code directories")

    sp = sub.add_parser("s1", help="Kconfig model")
    sp.add_argument("--force", action="store_true", help="regenerate even if cached")
    sp.add_argument("--dimacs", action="store_true", help="also write DIMACS (slow)")
    sp.add_argument("--no-repair", action="store_true",
                    help="use kclause's formula unrepaired (see kreach/s1_repair.py)")
    sp.add_argument("--r5", action="store_true",
                    help="also bound non-visible selected symbols above "
                         "(experimental; over-constrains on v6.17 x86_64)")
    sp.set_defaults(func=cmd_s1)

    sp = sub.add_parser("s2", help="Kbuild presence conditions")
    sp.add_argument("--force", action="store_true")
    add_subdir(sp)
    sp.set_defaults(func=cmd_s2)

    sp = sub.add_parser("s3", help="symbol references from compiled objects")
    sp.add_argument("--config", action="append",
                    help="wide configuration to build (default allmodconfig, allyesconfig)")
    sp.add_argument("--whole-tree", action="store_true",
                    help="build the whole tree instead of the selected subtrees")
    sp.add_argument("--skip-build", action="store_true",
                    help="harvest from an existing build tree only")
    sp.add_argument("--force", action="store_true")
    add_subdir(sp)
    sp.set_defaults(func=cmd_s3)

    sp = sub.add_parser("s4", help="reference conditions and candidate pairs")
    sp.add_argument("--max-candidates", type=int, default=None)
    sp.add_argument("--use-feedback", action="store_true",
                    help="skip pairs a previous S6 run rejected")
    sp.set_defaults(func=cmd_s4)

    sp = sub.add_parser("s5", help="solver query and witness generation")
    sp.add_argument("--case", action="append", choices=["y/m", "y/n", "m/n"],
                    help="failure case to query (default y/m, the main target)")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--no-witness", action="store_true",
                    help="solve only, do not materialise .config files")
    sp.add_argument("--caller", action="append",
                    help="only solve candidates with this caller option "
                         "(repeatable); for re-confirming a known set")
    sp.add_argument("--provider", action="append",
                    help="only solve candidates with this provider option "
                         "(repeatable)")
    sp.set_defaults(func=cmd_s5)

    sp = sub.add_parser("s6", help="build confirmation")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--parallel", type=int, default=1, help="witnesses built at once")
    sp.add_argument("--build-jobs", type=int, default=None, help="make -j per build")
    sp.add_argument("--keep-build", action="store_true",
                    help="keep object files (uses a lot of disk)")
    sp.add_argument("--ccache", action="store_true",
                    help="build with ccache; witnesses share most objects, and "
                         "they are pruned after each build, so this is where "
                         "the reuse comes from")
    sp.add_argument("--rebuild", action="store_true",
                    help="build every witness again instead of keeping the "
                         "results already on disk")
    sp.add_argument("--provider", action="append",
                    help="only confirm findings with this provider option "
                         "(repeatable); builds are the scarce resource and one "
                         "missing constraint usually breaks a whole subsystem")
    sp.add_argument("--caller", action="append",
                    help="only confirm findings with this caller option "
                         "(repeatable)")
    sp.add_argument("--no-reference-check", action="store_true",
                    help="do not compile the caller object first to check the "
                         "reference is still emitted under the witness")
    sp.set_defaults(func=cmd_s6)

    sp = sub.add_parser("s7", help="report and fix suggestion")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--publish", metavar="DIR",
                    help="also write a self-contained copy here: the report, "
                         "the records, the table, and each finding's witness "
                         "and build log, with paths rewritten to match")
    sp.set_defaults(func=cmd_s7)

    sp = sub.add_parser("run", help="S1..S7")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dimacs", action="store_true")
    sp.add_argument("--no-repair", action="store_true")
    sp.add_argument("--r5", action="store_true")
    sp.add_argument("--config", action="append")
    sp.add_argument("--whole-tree", action="store_true")
    sp.add_argument("--skip-build", action="store_true")
    sp.add_argument("--max-candidates", type=int, default=None)
    sp.add_argument("--use-feedback", action="store_true")
    sp.add_argument("--case", action="append", choices=["y/m", "y/n", "m/n"])
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--no-witness", action="store_true")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--parallel", type=int, default=1)
    sp.add_argument("--build-jobs", type=int, default=None)
    sp.add_argument("--keep-build", action="store_true")
    sp.add_argument("--ccache", action="store_true")
    sp.add_argument("--rebuild", action="store_true")
    sp.add_argument("--provider", action="append")
    sp.add_argument("--caller", action="append")
    sp.add_argument("--no-reference-check", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    add_subdir(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="what has been computed so far")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("test", help="stage acceptance tests")
    sp.add_argument("stage", choices=["s1", "s2"])
    sp.add_argument("--samples", type=int, default=20,
                    help="how many randconfigs (spec asks for 1000 for S1)")
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--config", action="append",
                    help="named configurations to check in S2 (default "
                         "allmodconfig, defconfig)")
    sp.add_argument("--reuse-s3-build", action="store_true",
                    help="check against the build S3 already made for that "
                         "configuration instead of making another one")
    add_subdir(sp)
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("randcompare",
                        help="how many randconfigs it takes to hit each finding")
    sp.add_argument("--samples", type=int, default=100)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_randcompare)

    sp = sub.add_parser("compare", help="mark findings still present / fixed / new")
    sp.add_argument("previous", help="a previous findings.jsonl")
    sp.add_argument("--current", help="defaults to this work dir's findings.jsonl")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("bench", help="build the historical-recall benchmark from git history")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--since", default="v6.0", help="git revision range start")
    sp.add_argument("-o", "--output")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("score", help="score findings against the mined benchmark")
    sp.add_argument("--benchmark", help="defaults to this work dir's benchmark.json")
    sp.add_argument("--findings", help="defaults to this work dir's findings.jsonl")
    sp.add_argument("--commit", help="score a single benchmark commit")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_score)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log)
    added = ensure_tool_path()
    if added:
        log.debug("added to PATH for the kmax commands: %s", added)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except WorkDirLocked as exc:
        log.error("%s", exc)
        return 3
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
