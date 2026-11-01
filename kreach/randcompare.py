"""Comparison with randconfig.

The spec's evaluation asks: run N randconfig builds against the same commit,
and compare which of the tool's findings randconfig also hits, and how many
builds that takes.

The second half of that question does not need the builds.  A randconfig hits
a finding exactly when its configuration satisfies the finding's own condition
-- the caller built in, the provider less available -- and that is a check
against the generated ``.config``, not a link.  So this runs N
``make randconfig`` invocations, which are seconds each, and reports for every
finding how many of them would have hit it.  From that comes the number the
comparison is really about: the expected number of random builds before this
bug turns up by chance.

Running the builds as well would confirm the hits the same way S6 does; it is
the same code path with the witness replaced by the randconfig, and it is
bounded by build capacity rather than by anything here.
"""

import os
import time

import z3

from kreach import formula as F
from kreach.config import Context
from kreach.s1_kconfig import config_to_assignment, parse_config_file
from kreach.util import get_logger, human_time, read_jsonl

log = get_logger("randcmp")


def finding_condition(finding: dict, table) -> z3.BoolRef | None:
    """The configuration condition under which this finding's build fails."""
    caller = finding["caller"]["object"]
    provider = finding["provider"]["object"]
    if caller not in table or provider not in table:
        return None
    c_builtin, c_avail = table.conditions(caller)
    p_builtin, p_avail = table.conditions(provider)
    return F.case_formula(finding["case"], c_builtin, c_avail, p_builtin, p_avail)


def run(ctx: Context, model, table, findings: list[dict], samples: int = 100,
        seed: int = 1) -> dict:
    t0 = time.time()
    conditions = {}
    for f in findings:
        cond = finding_condition(f, table)
        if cond is not None:
            conditions[f["id"]] = (f, cond)
    if not conditions:
        log.warning("no findings with a presence condition to check")
        return {"samples": 0, "findings": []}

    log.info("checking %d findings against %d randconfigs", len(conditions), samples)
    bdir = ctx.dir("randcompare")
    from kreach.s3_symbols import make

    hits: dict[str, int] = {fid: 0 for fid in conditions}
    checked = 0
    for i in range(samples):
        make(ctx, bdir, ["randconfig"], jobs=1, check=False,
             extra_env={"KCONFIG_SEED": hex(seed + i)})
        cfg = os.path.join(bdir, ".config")
        if not os.path.exists(cfg):
            continue
        checked += 1
        settings = parse_config_file(cfg)
        lits = config_to_assignment(settings, model)
        s = z3.Solver()
        s.set("timeout", 30000)
        s.add(*lits)
        for fid, (_f, cond) in conditions.items():
            s.push()
            s.add(cond)
            if s.check() == z3.sat:
                hits[fid] += 1
            s.pop()
        if (i + 1) % 10 == 0:
            log.info("  %d/%d randconfigs, %d findings hit at least once",
                     i + 1, samples, sum(1 for v in hits.values() if v))

    rows = []
    for fid, (f, _cond) in sorted(conditions.items()):
        n = hits[fid]
        rows.append({
            "id": fid,
            "case": f["case"],
            "caller_option": f["caller"]["option"],
            "provider_option": f["provider"]["option"],
            "randconfigs_hitting": n,
            "rate": round(n / checked, 4) if checked else None,
            # How many random builds you would expect to need to stumble on it.
            "expected_builds_to_hit": (round(checked / n, 1) if n else None),
        })
    rows.sort(key=lambda r: (-r["randconfigs_hitting"], r["id"]))
    never = [r for r in rows if not r["randconfigs_hitting"]]
    result = {
        "samples": checked,
        "findings": rows,
        "hit_by_randconfig": len(rows) - len(never),
        "missed_by_randconfig": len(never),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("randconfig comparison: %d of %d findings hit within %d "
             "randconfigs (%s)", result["hit_by_randconfig"], len(rows),
             checked, human_time(time.time() - t0))
    return result


def load_findings(ctx: Context) -> list[dict]:
    if not os.path.exists(ctx.findings_file):
        raise FileNotFoundError(
            "no findings at %s -- run 'kreach s7' first" % ctx.findings_file)
    return list(read_jsonl(ctx.findings_file))
