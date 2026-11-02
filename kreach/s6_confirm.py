# SPDX-License-Identifier: GPL-2.0-or-later
"""S6 -- Build confirmation.

Build every witness with an unmodified ``make`` and keep only findings that
fail exactly as predicted.

  Pass: ``undefined reference to '<s>'`` from the vmlinux link (cases y/m and
        y/n), or modpost reporting ``<s>`` undefined (case m/n).
  Fail: the build succeeds, or fails for another reason.  The pair is dropped
        and logged as a false positive, which feeds back into S4.
  Budget: one build per finding; witness minimisation in S5 keeps the builds
        short.

This is the stage that makes reported precision 100% by construction, so it
deliberately refuses to generalise: the predicted symbol must appear in the
predicted kind of error message, in this build, or the finding is dropped.

Before spending a build, each witness goes through two cheap checks.

The first re-evaluates the query against the `.config`.  A configuration can
satisfy the caller and provider levels and still not exercise the finding,
because a presence condition names more symbols than those two and Kconfig is
free to turn one off.  That costs a second and has saved a ten-minute link.

The second compiles *just the caller object* and looks for the symbol in its
undefined list.  This is the spec's "later versions: derive RC precisely by
compiling the caller under the variant configurations from S5", and it is
worth having early because witness minimisation works against RC accuracy:
the smaller the configuration, the more of the caller is compiled away, and
the less likely the reference survives.  `sound/soc/meson/aiu.o` under one
minimised witness came out 6.6 KB with no regmap reference at all, where
`allmodconfig` had emitted one -- so the vmlinux link succeeded and 217
seconds went nowhere.  Compiling the one object instead takes about forty.
"""

import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from kreach.config import Context
from kreach.util import get_logger, human_time, read_jsonl, write_jsonl

log = get_logger("s6")

# "foo.c:(.text+0x1): undefined reference to `bar_register'"
UNDEF_REF_RE = re.compile(r"undefined reference to [`'\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]?")
# "ERROR: modpost: \"bar_register\" [drivers/foo/foo.ko] undefined!"
MODPOST_RE = re.compile(r"modpost:\s*\"([A-Za-z_][A-Za-z0-9_.]*)\"[^\n]*undefined")

DEFAULT_TIMEOUT = 7200

# Build failures that say something about the machine rather than about the
# finding.  A missing generated header means the tree was disturbed, and the
# rest are resource exhaustion.  Recording any of them as a rejection would
# drop a real finding on the strength of a bad moment, so they get their own
# non-terminal status and are retried.
ENVIRONMENT_FAILURES = (
    "autoconf.h: No such file or directory",
    "No space left on device",
    "Cannot allocate memory",
    "virtual memory exhausted",
    "cc1: out of memory",
    "Killed signal terminated program",
    "collect2: fatal error: ld terminated with signal",
    "Resource temporarily unavailable",
    # make reports a SIGKILLed child as 128+9.  The usual cause is the OOM
    # killer taking ld during the vmlinux link, which wants a couple of
    # gigabytes of its own -- seen here as
    #   Out of memory: Killed process (ld) ... anon-rss:2485076kB
    # Nothing about the finding, so it must not be recorded as a verdict.
    "Error 137",
)

# Verdicts that settle a witness.  Anything else -- an interrupted build, a
# make that fell over before linking -- leaves the question open and is
# retried on the next run.
TERMINAL = frozenset({
    "confirmed",
    "rejected_build_ok",
    "rejected_other_undefined",
    "rejected_other_error",
    "skipped_witness_defanged",
    "skipped_reference_not_emitted",
    "no_witness",
})


def _targets_for_case(case: str) -> list[str]:
    if case == "m/n":
        return ["modules"]
    return ["vmlinux"]


def _scan_log(text: str) -> tuple[set[str], set[str]]:
    """(symbols in undefined-reference errors, symbols modpost called undefined)"""
    return (set(UNDEF_REF_RE.findall(text)), set(MODPOST_RE.findall(text)))


def _relevant_lines(text: str, symbols: set[str], limit: int = 20) -> list[str]:
    out = []
    for line in text.splitlines():
        if "undefined" not in line:
            continue
        if any(s in line for s in symbols):
            out.append(line.strip())
            if len(out) >= limit:
                break
    return out


def ccache_vars(ctx: Context, enabled: bool) -> dict:
    """``CC="ccache gcc"`` for the witness builds.

    Witnesses are minimal configurations that differ in a handful of options,
    so successive builds compile mostly the same translation units.  Since the
    object files are pruned after each build to keep disk bounded, a shared
    compiler cache is what makes the second and later builds cheap.
    """
    if not enabled:
        return {}
    import shutil

    if not shutil.which("ccache"):
        log.warning("--ccache asked for but ccache is not installed; "
                    "building without it")
        return {}
    cc = os.environ.get("KREACH_CC", "gcc")
    cross = ctx.make_env().get("CROSS_COMPILE", "")
    return {"CC": "ccache %s%s" % (cross, cc)}


def witness_still_exercises(ctx: Context, rec: dict, model, table) -> tuple[bool, str]:
    """Does this witness still satisfy the query it was generated for?

    Re-derives the finding's condition from the two objects and the case, and
    evaluates it against the witness `.config`.  Returns (ok, reason).
    """
    if model is None or table is None:
        return True, "not checked"
    from kreach.randcompare import finding_condition
    from kreach.s1_kconfig import config_to_assignment, parse_config_file

    witness = os.path.join(ctx.work, rec["witness"])
    if not os.path.exists(witness):
        return True, "no witness to check"
    shaped = {"case": rec["case"],
              "caller": {"object": rec["caller"]},
              "provider": {"object": rec["provider"]}}
    cond = finding_condition(shaped, table)
    if cond is None:
        return True, "no presence condition"
    import z3 as _z3

    from kreach import formula as _F

    solver = _z3.Solver()
    solver.set("timeout", 60000)
    solver.add(*config_to_assignment(parse_config_file(witness), model,
                                     symbols=_F.collect_config_symbols([cond])))
    solver.add(cond)
    res = solver.check()
    if res == _z3.sat:
        return True, "ok"
    return False, ("the witness no longer satisfies the query (%s): a symbol "
                   "in one of the presence conditions is off, so nothing "
                   "would be compiled" % res)


def reference_still_emitted(ctx: Context, rec: dict,
                            jobs: int | None = None,
                            make_vars: dict | None = None,
                            timeout: int = 1800) -> tuple[bool, str]:
    """Compile the caller object under the witness and look for the symbol.

    RC(c, s) is an approximation: S4 assumes the reference is emitted whenever
    the caller is built and the provider is enabled.  Under a minimised
    configuration that is often false, because the code around the call is
    compiled out.  Building the single object and reading its undefined
    symbols settles it for the price of one compile.

    Returns (ok, reason).  `ok` is True when at least one predicted symbol is
    still undefined in the object, and also when the check cannot be made --
    an inconclusive check must not drop a finding.
    """
    from kreach.s3_symbols import make

    caller = rec.get("caller")
    wanted = set(rec.get("symbols") or ())
    if not caller or not caller.endswith(".o") or not wanted:
        return True, "nothing to check"
    witness = os.path.join(ctx.work, rec["witness"])
    if not os.path.exists(witness):
        return True, "no witness"

    bdir = ctx.dir("s6", "refcheck", rec.get("query_id") or rec["id"])
    try:
        shutil.rmtree(bdir, ignore_errors=True)
        os.makedirs(bdir, exist_ok=True)
        shutil.copyfile(witness, os.path.join(bdir, ".config"))
        make(ctx, bdir, ["olddefconfig"], jobs=1, check=False)
        proc = make(ctx, bdir, [caller], jobs=jobs, check=False,
                    timeout=timeout, make_vars=make_vars)
        obj = os.path.join(bdir, caller)
        if proc.returncode != 0 or not os.path.exists(obj):
            return True, "could not build %s on its own; building in full" % caller
        undefined = {name for _p, symtype, name in _nm_undefined([obj])}
        hit = wanted & undefined
        if hit:
            return True, "emits %s" % ", ".join(sorted(hit))
        return False, ("%s does not reference %s in this configuration, so the "
                       "link cannot fail on it" % (caller, ", ".join(sorted(wanted))))
    except Exception as exc:
        return True, "check failed (%s); building in full" % exc
    finally:
        shutil.rmtree(bdir, ignore_errors=True)


def _nm_undefined(paths):
    from kreach.s3_symbols import _nm_batch

    for path, symtype, name in _nm_batch(paths):
        if symtype == "U":
            yield path, symtype, name


def confirm_one(ctx: Context, rec: dict, timeout: int = DEFAULT_TIMEOUT,
                jobs: int | None = None, keep_build: bool = False,
                make_vars: dict | None = None) -> dict:
    """Build one witness and classify the outcome."""
    from kreach.s3_symbols import make

    # The build is keyed by the query, not the finding: two queries can share
    # a finding id but have different witnesses (see query_id in s5_solve).
    fid = rec.get("query_id") or rec["id"]
    case = rec["case"]
    out = dict(rec)
    bdir = ctx.s6_build_dir(fid)
    logfile = ctx.s6_log_file(fid)
    witness = os.path.join(ctx.work, rec["witness"])
    if not os.path.exists(witness):
        out.update(status="no_witness", detail="missing %s" % witness)
        return out

    # Start from a clean directory.  Builds are pruned afterwards, so anything
    # left here is the debris of a build that did not finish -- and a
    # half-written object tree breaks the next make in ways that look like a
    # compile error (`fatal error: opening dependency file ...`).
    if os.path.isdir(bdir):
        shutil.rmtree(bdir, ignore_errors=True)
    os.makedirs(bdir, exist_ok=True)
    shutil.copyfile(witness, os.path.join(bdir, ".config"))

    t0 = time.time()
    try:
        # olddefconfig is a no-op on a config Kconfig itself produced; it is
        # here so that the build starts from a config make considers current.
        make(ctx, bdir, ["olddefconfig"], jobs=1, check=False, logfile=logfile)
        proc = make(ctx, bdir, _targets_for_case(case), jobs=jobs,
                    check=False, timeout=timeout, logfile=logfile,
                    make_vars=make_vars)
        rc = proc.returncode
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:
        out.update(status="build_error", detail=str(exc)[:2000],
                   build_seconds=round(time.time() - t0, 1))
        return out

    seconds = round(time.time() - t0, 1)
    undef, modpost = _scan_log(text)
    wanted = set(rec["symbols"])
    hit_link = wanted & undef
    hit_modpost = wanted & modpost

    out["build_seconds"] = seconds
    out["build_rc"] = rc
    out["build_log"] = os.path.relpath(logfile, ctx.work)

    environmental = [s for s in ENVIRONMENT_FAILURES if s in text]
    if environmental:
        # The build fell over for a reason that has nothing to do with the
        # symbol.  Leave the question open rather than answering it wrongly.
        out["status"] = "build_environment_error"
        out["detail"] = "%s; retried on the next run" % environmental[0]
    elif rc is not None and rc < 0:
        # Terminated by a signal -- the machine was interrupted, the build was
        # not.  This says nothing about the finding, so it must not be
        # recorded as a rejection: that would drop a real finding and feed a
        # false entry back into S4.  Left unresolved so a later run retries.
        out["status"] = "build_interrupted"
        out["detail"] = "killed by signal %d after %ss" % (-rc, seconds)
    elif case in ("y/m", "y/n") and hit_link:
        out["status"] = "confirmed"
        out["failing_symbols"] = sorted(hit_link)
        out["build_log_lines"] = _relevant_lines(text, hit_link)
    elif case == "m/n" and hit_modpost:
        out["status"] = "confirmed"
        out["failing_symbols"] = sorted(hit_modpost)
        out["build_log_lines"] = _relevant_lines(text, hit_modpost)
    elif rc == 0:
        out["status"] = "rejected_build_ok"
        out["detail"] = "build succeeded; the predicted link error did not occur"
    elif undef or modpost:
        # It failed on undefined symbols, but not the ones we predicted.
        out["status"] = "rejected_other_undefined"
        out["detail"] = "undefined symbols not predicted: %s" % (
            ", ".join(sorted((undef | modpost) - wanted))[:500])
        out["build_log_lines"] = _relevant_lines(text, (undef | modpost))
    else:
        out["status"] = "rejected_other_error"
        out["detail"] = _error_tail(text)

    if not keep_build:
        # Builds are big; keep the log, drop the objects.
        _prune_build(bdir)
    return out


def _error_tail(text: str, limit: int = 1500) -> str:
    lines = [l for l in text.splitlines()
             if re.search(r"\berror\b|\bError\b|^make.*\*\*\*", l)]
    if not lines:
        lines = text.splitlines()[-20:]
    return "\n".join(lines[-20:])[:limit]


def _prune_build(bdir: str) -> None:
    keep = {"build.log", ".config", "kreach.partial"}
    for name in os.listdir(bdir):
        if name in keep:
            continue
        path = os.path.join(bdir, name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass


def _key(rec: dict) -> tuple:
    return (rec.get("query_id") or rec["id"], rec["case"])


def run_s6(ctx: Context, queries: list[dict], limit: int | None = None,
           timeout: int = DEFAULT_TIMEOUT, parallel: int = 1,
           jobs: int | None = None, keep_build: bool = False,
           ccache: bool = False, rebuild: bool = False,
           model=None, table=None, providers=None,
           callers=None, check_reference: bool = True) -> list[dict]:
    t0 = time.time()
    make_vars = ccache_vars(ctx, ccache)
    if make_vars:
        log.info("witness builds will use %s", make_vars["CC"])
    todo = [q for q in queries if q.get("status") == "sat" and q.get("witness")]
    # Builds are the scarce resource, and one missing constraint usually
    # breaks many callers of the same subsystem, so it is worth being able to
    # settle a whole provider option first rather than working through the
    # queue in whatever order it came out of S5.
    if providers:
        wanted = set(providers)
        before = len(todo)
        todo = [q for q in todo if q.get("provider_option") in wanted]
        log.info("provider filter: %d of %d satisfiable queries match %s",
                 len(todo), before, ", ".join(sorted(wanted)))
    if callers:
        wanted = set(callers)
        before = len(todo)
        todo = [q for q in todo if q.get("caller_option") in wanted]
        log.info("caller filter: %d of %d queries match %s",
                 len(todo), before, ", ".join(sorted(wanted)))
    # One build per finding; dedupe by witness id so a pair that shows up in
    # several cases is only built once per case.
    seen = set()
    unique = []
    for q in todo:
        key = _key(q)
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)

    # The budget is one build per finding, so results accumulate across runs:
    # a bounded run today and the rest tomorrow must not throw away what was
    # already built.  Pass --rebuild to ignore what is on disk.
    # Only terminal verdicts count as done.  A build that was interrupted or
    # errored out before it could say anything is retried.
    previous = {} if rebuild else {
        _key(r): r for r in load_results(ctx) if r.get("status") in TERMINAL}
    if previous:
        log.info("S6: %d witnesses already built in a previous run", len(previous))
    pending = [q for q in unique if _key(q) not in previous]
    if limit:
        pending = pending[:limit]
    log.info("S6: %d witnesses to build now (%d satisfiable queries, %d distinct, "
             "%d already done), parallel=%d, -j%d each",
             len(pending), len(todo), len(unique), len(previous), parallel,
             jobs or ctx.jobs)

    results: list[dict] = list(previous.values())
    unique = pending
    if parallel <= 1:
        for i, rec in enumerate(unique, 1):
            ok, why = witness_still_exercises(ctx, rec, model, table)
            if not ok:
                log.warning("[%d/%d] skipping witness %s: %s", i, len(unique),
                            rec.get("query_id") or rec["id"], why)
                res = dict(rec)
                res.update(status="skipped_witness_defanged", detail=why)
                results.append(res)
                write_jsonl(ctx.confirm_file, results)
                continue
            if check_reference:
                ok, why = reference_still_emitted(ctx, rec, jobs=jobs,
                                                  make_vars=make_vars)
                if not ok:
                    log.warning("[%d/%d] skipping witness %s: %s", i, len(unique),
                                rec.get("query_id") or rec["id"], why)
                    res = dict(rec)
                    res.update(status="skipped_reference_not_emitted", detail=why)
                    results.append(res)
                    write_jsonl(ctx.confirm_file, results)
                    continue
                log.debug("    caller still %s", why)
            if rec.get("witness_size_outlier"):
                log.warning("    witness has %d settings enabled against a "
                            "median of %d; this is a much larger kernel and "
                            "may exhaust memory or disk",
                            rec.get("witness_symbols_on"),
                            rec.get("witness_median"))
            log.info("[%d/%d] building witness %s (%s, %s -> %s, %s settings)",
                     i, len(unique), rec.get("query_id") or rec["id"],
                     rec["case"], rec["caller_option"], rec["provider_option"],
                     rec.get("witness_symbols_on", "?"))
            res = confirm_one(ctx, rec, timeout=timeout, jobs=jobs,
                              keep_build=keep_build, make_vars=make_vars)
            log.info("    -> %s (%ss)", res["status"], res.get("build_seconds"))
            results.append(res)
            write_jsonl(ctx.confirm_file, results)
    else:
        per = max(1, (jobs or ctx.jobs) // parallel)
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futs = {pool.submit(confirm_one, ctx, rec, timeout, per,
                                keep_build, make_vars): rec
                    for rec in unique}
            for n, fut in enumerate(as_completed(futs), 1):
                res = fut.result()
                log.info("[%d/%d] %s -> %s (%ss)", n, len(unique), res.get("id"),
                         res["status"], res.get("build_seconds"))
                results.append(res)
                write_jsonl(ctx.confirm_file, results)

    write_jsonl(ctx.confirm_file, results)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    confirmed = by_status.get("confirmed", 0)
    built = [r for r in results
             if r["status"] not in ("skipped_witness_defanged",
                                    "skipped_reference_not_emitted",
                                    "build_interrupted", "build_error",
                                    "build_environment_error")]
    ctx.write_meta({"s6": {
        "built": len(built),
        "records": len(results),
        "skipped_witness_defanged": by_status.get("skipped_witness_defanged", 0),
        "skipped_reference_not_emitted":
            by_status.get("skipped_reference_not_emitted", 0),
        "unresolved": sum(v for k, v in by_status.items() if k not in TERMINAL),
        "built_this_run": len(unique),
        "satisfiable_queries": len(todo),
        "by_status": by_status,
        "confirmed": confirmed,
        "candidate_precision": (round(confirmed / len(built), 3) if built else None),
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S6 complete: %d confirmed of %d built (%s) in %s",
             confirmed, len(built),
             ", ".join("%s=%d" % kv for kv in sorted(by_status.items())),
             human_time(time.time() - t0))
    return results


def load_results(ctx: Context) -> list[dict]:
    return list(read_jsonl(ctx.confirm_file))
