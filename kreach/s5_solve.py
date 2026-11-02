# SPDX-License-Identifier: GPL-2.0-or-later
"""S5 -- solve the core property per candidate and build a witness.

Query is ``K and RC(c,s) and level(c) > level(p)``, one case at a time.
Writes s5/queries.jsonl and s5/witness/<id>.config.

The witness is minimised (see Minimiser) and then materialised by Kconfig
itself, via ``make KCONFIG_ALLCONFIG=<fragment> allnoconfig``, so S6 gets a
file an unmodified make produced.  It is checked twice before being kept: the
caller and provider must still be at the queried levels, and the whole query
must still hold under the result -- a presence condition names more symbols
than those two, and Kconfig is free to turn one off.

docs/design.md has the reasoning.
"""

import hashlib
import os
import time

import z3

from kreach import formula as F
from kreach.config import Context
from kreach.s1_kconfig import config_to_assignment, parse_config_file
from kreach.s4_refcond import rc_formula
from kreach.util import get_logger, human_time, read_jsonl, write_jsonl

log = get_logger("s5")

MAX_CORE_ROUNDS = 400

# A witness far larger than its peers may not build at all on a small
# machine.  Flag it before S6 spends the time finding out.
OUTLIER_RATIO = 3.0
OUTLIER_FLOOR = 1500


def load_realisability(ctx: Context) -> list:
    """Constraints that make a model writable as a .config file.

    K is what Kconfig accepts.  A witness needs more: it has to survive being
    handed to Kconfig as a file.  A symbol with no prompt cannot be set from a
    .config at all -- Kconfig recomputes it from what selects it -- and about
    a sixth of them have none.  S1 keeps those constraints in a file of their
    own because they are too strong for K itself (docs/design.md, R5).
    """
    path = os.path.join(ctx.s1_dir, "realisable.smt2")
    if not os.path.exists(path):
        log.warning("no %s; witnesses may set symbols Kconfig cannot, and be "
                    "rejected.  Re-run 'kreach s1'.", path)
        return []
    with open(path) as fh:
        cons = list(z3.parse_smt2_string(fh.read()))
    log.info("realisability: %d constraints keeping witnesses writable as a "
             ".config", len(cons))
    return cons


def finding_id(caller_opt: str, provider_opt: str, symbols: list[str]) -> str:
    """Stable hash of caller option, provider option and symbol.

    This is the record id the report carries, so it stays the same across
    runs and kernel commits -- that is what lets `kreach compare` mark a
    finding still present, fixed or new.
    """
    h = hashlib.sha256()
    h.update(("%s|%s|%s" % (caller_opt, provider_opt, ",".join(sorted(symbols)))).encode())
    return h.hexdigest()[:12]


def query_id(cand: dict, table) -> str:
    """Hash identifying one *query*, used for its witness and build directory.

    Two queries can share a finding id -- same options, same symbols, but
    different presence conditions, for instance a caller compiled from two
    directories.  They are different questions with different witnesses, so
    they must not write to the same file; otherwise one finding would be
    confirmed against the other's build.
    """
    raw = table.raw()
    c_rec = raw.get(cand["caller"], {})
    p_rec = raw.get(cand["provider"], {})
    h = hashlib.sha256()
    h.update(("%s|%s|%s|%s|%s|%s|%s" % (
        cand.get("caller_option"), cand.get("provider_option"),
        ",".join(sorted(cand["symbols"])), cand.get("rc_rule"),
        c_rec.get("builtin", ""), c_rec.get("available", ""),
        p_rec.get("builtin", ""))).encode())
    return h.hexdigest()[:12]


class Minimiser:
    """Solve a query while keeping as few symbols enabled as possible."""

    def __init__(self, model, base_config: dict | None = None,
                 timeout_ms: int = 120000, extra_constraints=None,
                 max_passes: int = 4, max_checks: int = 400):
        self.model = model
        self.symbols = sorted(model.bool_tristate)
        self.base_config = base_config or {}
        self.max_passes = max_passes
        # Solver calls per shrinking pass.  Each is a fraction of a second;
        # the budget stops one pathological witness eating the sweep.
        self.max_checks = max_checks
        self._solver = z3.Solver()
        self._solver.set("timeout", timeout_ms)
        # ALWAYS_FALSE: prefer every symbol off, so the first model found is
        # already close to allnoconfig and phase 2 has little left to do.
        self._solver.set("phase_selection", 2)
        self._solver.add(*model.constraints)
        if extra_constraints:
            self._solver.add(*extra_constraints)
        self._solver.push()  # keep the base model on the stack

    def _enabled_in(self, m) -> list[str]:
        return [sym for sym in self.symbols
                if z3.is_true(m.eval(F.enabled_var(sym), model_completion=True))]

    def solve(self, query) -> tuple[str, z3.ModelRef | None, dict]:
        """Returns (status, model, info)."""
        s = self._solver
        s.pop()
        s.push()
        s.add(query)
        t0 = time.time()
        res = s.check()
        if res == z3.unsat:
            return "unsat", None, {"seconds": round(time.time() - t0, 2)}
        if res == z3.unknown:
            return "unknown", None, {"reason": s.reason_unknown(),
                                     "seconds": round(time.time() - t0, 2)}

        m = s.model()
        initial_on = self._enabled_in(m)
        t_phase1 = time.time() - t0

        # One shrinking pass stops at the first satisfying assignment, which
        # need not be a small one, so repeat until it stops shrinking.
        best, on = m, list(initial_on)
        rounds = released = 0
        passes = 0
        sizes = [len(on)]
        why = "not attempted"
        while passes < self.max_passes:
            passes += 1
            model, r, rel, why = self._shrink(s, on)
            # Count the work before deciding what to do with it: a failed
            # pass must not report as having done nothing.
            rounds += r
            released += rel
            if model is None:
                break
            new_on = self._enabled_in(model)
            if len(new_on) >= len(on):
                why = "no further improvement"
                best = model
                break
            best, on = model, new_on
            sizes.append(len(on))

        on = self._enabled_in(best)
        return "sat", best, {
            "checks": rounds,
            "turned_off": released,
            "passes": passes,
            "sizes": sizes,
            "stopped_because": why,
            "symbols_on_initial": len(initial_on),
            "symbols_on": len(on),
            "phase1_seconds": round(t_phase1, 2),
            "seconds": round(time.time() - t0, 2),
        }

    def _shrink(self, s, candidates):
        """Turn off as many of `candidates` as the constraints allow.

        Returns (model, checks, turned_off, why).

        Removal is by block: assume a whole block off, and if that holds the
        block goes in one call, otherwise split and try the halves.  A block
        that fails at size one is required.  Releasing one unsat core at a
        time instead costs a solver call per symbol, which does not scale past
        a few hundred candidates.

        Partial progress is kept if the budget runs out.
        """
        off: list[str] = []
        best = None
        checks = 0
        queue = [list(candidates)]
        why = "nothing to do"
        while queue and checks < self.max_checks:
            block = queue.pop(0)
            if not block:
                continue
            checks += 1
            assumptions = [z3.Not(F.enabled_var(sym)) for sym in off + block]
            res = s.check(*assumptions)
            if res == z3.sat:
                off.extend(block)
                best = s.model()
                why = "satisfied"
            elif res == z3.unknown:
                why = "solver gave up: %s" % s.reason_unknown()
                break
            elif len(block) > 1:
                mid = len(block) // 2
                queue.insert(0, block[mid:])
                queue.insert(0, block[:mid])
            # A single symbol that cannot be turned off is required; drop it.
        if queue and checks >= self.max_checks:
            why = "hit the check budget with %d block(s) unexamined" % len(queue)
        return best, checks, len(off), why


def filter_candidates(candidates: list[dict], callers=None,
                      providers=None) -> list[dict]:
    """Narrow the candidate list to particular options.

    Re-confirming a known set of findings against another revision does not
    need the whole sweep solved -- only the pairs already in hand.  The same
    filters exist on S6, which spends the builds; having them here too is
    what makes a targeted re-run minutes rather than an hour.
    """
    out = candidates
    if callers:
        wanted = set(callers)
        out = [c for c in out if c.get("caller_option") in wanted]
        log.info("caller filter: %d of %d candidates match %s",
                 len(out), len(candidates), ", ".join(sorted(wanted)))
    if providers:
        wanted = set(providers)
        before = len(out)
        out = [c for c in out if c.get("provider_option") in wanted]
        log.info("provider filter: %d of %d candidates match %s",
                 len(out), before, ", ".join(sorted(wanted)))
    return out


def collapse_candidates(candidates: list[dict], table) -> list[dict]:
    """One query per distinct problem, not per object pair.

    Thousands of object pairs reduce to a few hundred questions, because the
    query only looks at the two presence conditions and the two Kconfig
    options.  Two candidates with the same conditions and options ask the
    solver the identical question, so they are merged and their symbols and
    objects collected -- which is also what the report wants, since one
    missing constraint breaks every caller of the subsystem at once.
    """
    raw = table.raw()
    groups: dict[tuple, dict] = {}
    for cand in candidates:
        c_rec = raw.get(cand["caller"])
        p_rec = raw.get(cand["provider"])
        if c_rec is None or p_rec is None:
            continue
        key = (cand.get("caller_option"), cand.get("provider_option"),
               cand.get("rc_rule"), c_rec["builtin"], c_rec["available"],
               p_rec["builtin"], p_rec["available"])
        group = groups.get(key)
        if group is None:
            group = dict(cand)
            group["symbols"] = list(cand["symbols"])
            group["caller_objects"] = [cand["caller"]]
            group["provider_objects"] = [cand["provider"]]
            groups[key] = group
            continue
        for sym in cand["symbols"]:
            if sym not in group["symbols"]:
                group["symbols"].append(sym)
        for field, value in (("caller_objects", cand["caller"]),
                             ("provider_objects", cand["provider"])):
            if value not in group[field]:
                group[field].append(value)
    out = list(groups.values())
    for g in out:
        g["symbols"] = sorted(g["symbols"])
        g["caller_objects"] = sorted(g["caller_objects"])
        g["provider_objects"] = sorted(g["provider_objects"])
    log.info("collapsed %d candidate pairs into %d distinct queries",
             len(candidates), len(out))
    return out


def model_to_partial_config(model: z3.ModelRef, kmodel, base: dict | None = None) -> str:
    """Emit the enabled part of a model as a Kconfig fragment.

    Only symbols that are on are written, plus non-boolean values taken from
    the base config, so that ``allnoconfig`` turns everything else off.
    """
    lines = ["# kreach witness fragment (consumed via KCONFIG_ALLCONFIG)"]
    for sym in sorted(kmodel.bool_tristate):
        level = F.model_level(model, sym)
        if level == F.LEVEL_Y:
            lines.append("%s=y" % sym)
        elif level == F.LEVEL_M:
            lines.append("%s=m" % sym)
    if base:
        for sym, val in sorted(base.items()):
            if kmodel.kconfig_types.get(sym) in ("int", "hex", "string") and val not in ("n", ""):
                lines.append("%s=%s" % (sym, val))
    return "\n".join(lines) + "\n"


def materialise_witness(ctx: Context, fragment: str, out_config: str,
                        builddir: str) -> tuple[bool, dict, str]:
    """Run the kernel's Kconfig over the fragment to get a full .config."""
    from kreach.s3_symbols import make

    os.makedirs(builddir, exist_ok=True)
    partial = os.path.join(builddir, "kreach.partial")
    with open(partial, "w") as fh:
        fh.write(fragment)
    logtext = []
    try:
        proc = make(ctx, builddir, ["allnoconfig"],
                    extra_env={"KCONFIG_ALLCONFIG": partial}, jobs=1, check=False)
        logtext.append(proc.stdout or "")
        logtext.append(proc.stderr or "")
    except Exception as exc:  # pragma: no cover - make missing/broken tree
        return False, {}, str(exc)
    produced = os.path.join(builddir, ".config")
    if not os.path.exists(produced):
        return False, {}, "\n".join(logtext)[-4000:]
    os.makedirs(os.path.dirname(os.path.abspath(out_config)), exist_ok=True)
    with open(produced) as src, open(out_config, "w") as dst:
        dst.write(src.read())
    return True, parse_config_file(out_config), "\n".join(logtext)[-4000:]


def check_levels(settings: dict, expected: dict) -> tuple[bool, dict]:
    """Did Kconfig keep the levels the query asked for?"""
    actual = {sym: settings.get(sym, "n") for sym in expected}
    bad = {sym: (want, actual[sym]) for sym, want in expected.items()
           if actual[sym] != want}
    return (not bad), {"expected": expected, "actual": actual, "mismatch": bad}


def check_query_holds(settings: dict, model, query) -> tuple[bool, dict]:
    """Does the configuration Kconfig produced still satisfy the query?

    Checking the caller and provider options is not enough.  The query also
    constrains everything else in the two presence conditions, and Kconfig is
    free to turn one of those off:

        snd-soc-sdca-$(CONFIG_SND_SOC_SDCA_HID) += sdca_hid.o

    The caller here is `sdca_hid.o`, whose condition needs
    `CONFIG_SND_SOC_SDCA_HID` as well as `CONFIG_SND_SOC_SDCA`.  With both
    named options at the levels the query asked for but that guard off, the
    object is never compiled, the reference is never emitted, and the build
    succeeds -- which cost a ten-minute `vmlinux` link to discover.  So the
    whole query is re-evaluated against the concrete configuration instead.
    """
    # Only the symbols the query mentions can change the answer: everything
    # else is unconstrained in the formula, so assigning it is wasted work.
    # Assigning all 18,000 symbols here showed up on S5's hot path, since
    # this runs for every witness.
    wanted = F.collect_config_symbols([query])
    lits = config_to_assignment(settings, model, symbols=wanted)
    solver = z3.Solver()
    solver.set("timeout", 60000)
    solver.add(*lits)
    solver.add(query)
    res = solver.check()
    return res == z3.sat, {"result": str(res), "symbols_fixed": len(wanted)}


def run_s5(ctx: Context, model, table, candidates: list[dict],
           cases=F.CASES, limit: int | None = None,
           skip_materialise: bool = False) -> list[dict]:
    t0 = time.time()
    base_config = {}
    scratch = ctx.dir("s5", "kconfig")
    # An allnoconfig for this arch gives us the non-boolean defaults.
    try:
        from kreach.s3_symbols import make
        make(ctx, scratch, ["allnoconfig"], jobs=1, check=False)
        if os.path.exists(os.path.join(scratch, ".config")):
            base_config = parse_config_file(os.path.join(scratch, ".config"))
            log.info("base allnoconfig: %d settings", len(base_config))
    except Exception as exc:
        log.warning("could not produce a base allnoconfig: %s", exc)

    realisable = load_realisability(ctx)
    # Facts about this architecture: an option its Kconfig never declares
    # cannot be set, so its level variables are all false.  Without this an
    # object guarded by such an option looks like it could be "built but not
    # built in", which reads as a provider at m.
    undefined = F.undefined_symbols_off(table.undefined, model.kconfig_types)
    if undefined:
        log.info("%d symbols this architecture does not declare are pinned off",
                 len(table.undefined))
    mini = Minimiser(model, base_config,
                     extra_constraints=realisable + undefined)
    results: list[dict] = []

    # Batching: group by provider option, since one missing constraint usually
    # breaks many callers of the same subsystem.
    candidates = collapse_candidates(candidates, table)
    candidates = sorted(candidates, key=lambda c: (c.get("provider_option") or "",
                                                   c.get("caller_option") or ""))
    seen_sat_pairs: set[tuple] = set()
    nsat = 0

    for idx, cand in enumerate(candidates, 1):
        if limit and len(results) >= limit:
            log.info("reached --limit=%d", limit)
            break
        try:
            c_builtin, c_avail = table.conditions(cand["caller"])
            p_builtin, p_avail = table.conditions(cand["provider"])
        except KeyError:
            continue
        rc, _ = rc_formula(cand, table, "y/m")

        for case in cases:
            core = F.case_formula(case, c_builtin, c_avail, p_builtin, p_avail)
            query = z3.And(rc, core)
            status, m, info = mini.solve(query)
            rec = {
                "case": case,
                "caller": cand["caller"],
                "caller_option": cand["caller_option"],
                "caller_source": cand.get("caller_source"),
                "provider": cand["provider"],
                "provider_option": cand["provider_option"],
                "provider_module": cand.get("provider_module"),
                "symbols": cand["symbols"],
                "caller_objects": cand.get("caller_objects", [cand["caller"]]),
                "provider_objects": cand.get("provider_objects", [cand["provider"]]),
                "rc_rule": cand.get("rc_rule"),
                "status": status,
                "solver": info,
                "arch": ctx.arch,
            }
            if status != "sat":
                results.append(rec)
                continue

            fid = finding_id(cand["caller_option"], cand["provider_option"],
                             cand["symbols"])
            qid = query_id(cand, table)
            rec["id"] = fid
            rec["query_id"] = qid
            expected = {}
            for sym in (cand["caller_option"], cand["provider_option"]):
                lvl = F.model_level(m, sym)
                expected[sym] = F.LEVEL_NAMES[lvl]
            rec["levels"] = expected
            rec["symbols_on"] = info.get("symbols_on")

            if skip_materialise:
                rec["status"] = "sat"
                results.append(rec)
                nsat += 1
                continue

            fragment = model_to_partial_config(m, model, base_config)
            witness = ctx.witness_file(qid)
            ok, settings, logtail = materialise_witness(
                ctx, fragment, witness, ctx.dir("s5", "kconfig"))
            if not ok:
                rec["status"] = "witness_failed"
                rec["detail"] = logtail
                results.append(rec)
                continue
            kept, detail = check_levels(settings, expected)
            rec["witness"] = os.path.relpath(witness, ctx.work)
            rec["witness_levels"] = detail
            rec["witness_symbols_on"] = sum(
                1 for v in settings.values() if v in ("y", "m"))
            if kept:
                # The named options survived; now check that the rest of the
                # query did too.
                kept, qdetail = check_query_holds(settings, model, query)
                rec["witness_query"] = qdetail
                if not kept:
                    detail = dict(detail)
                    detail["mismatch"] = {
                        "query": "configuration no longer satisfies the query; "
                                 "some symbol in a presence condition was turned off"}
            if not kept:
                # Kconfig disagreed with our model: an S1 encoding gap, not a
                # finding.  Report it rather than burning a build on it.
                rec["status"] = "witness_rejected"
                log.warning("witness %s rejected: kconfig gave %s", qid, detail["mismatch"])
            else:
                rec["status"] = "sat"
                nsat += 1
                seen_sat_pairs.add((cand["caller_option"], cand["provider_option"]))
            results.append(rec)
        if idx % 25 == 0:
            # Checkpoint: this stage runs for the better part of an hour, so
            # its results should not exist only in memory.
            write_jsonl(ctx.queries_file, results)
            log.info("S5 %d/%d candidates, %d satisfiable so far",
                     idx, len(candidates), nsat)

    mark_size_outliers(results)
    write_jsonl(ctx.queries_file, results)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    # Distinct queries that share a finding id: expected and harmless, since
    # the witness and build are keyed by query_id, but worth reporting.
    by_fid: dict[str, set] = {}
    for r in results:
        if r.get("status") == "sat":
            by_fid.setdefault(r["id"], set()).add(r.get("query_id"))
    shared = {k: len(v) for k, v in by_fid.items() if len(v) > 1}
    ctx.write_meta({"s5": {
        "queries": len(results),
        "by_status": by_status,
        "sat": nsat,
        "finding_ids_with_several_queries": len(shared),
        "provider_options_affected": len({p for _, p in seen_sat_pairs}),
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S5 complete: %d queries, %s, in %s", len(results),
             ", ".join("%s=%d" % kv for kv in sorted(by_status.items())),
             human_time(time.time() - t0))
    return results


def mark_size_outliers(results: list[dict], ratio: float = OUTLIER_RATIO,
                       floor: int = OUTLIER_FLOOR) -> list[dict]:
    """Flag witnesses far larger than their peers.

    Minimisation stops when the constraints are satisfied, which is not a
    guarantee of smallness; a witness several times the median is a kernel
    several times the size.
    """
    sizes = sorted(r["witness_symbols_on"] for r in results
                   if r.get("status") == "sat" and r.get("witness_symbols_on"))
    if not sizes:
        return results
    median = sizes[len(sizes) // 2]
    threshold = max(floor, median * ratio)
    flagged = []
    for r in results:
        n = r.get("witness_symbols_on")
        if r.get("status") != "sat" or not n:
            continue
        r["witness_median"] = median
        r["witness_size_outlier"] = n > threshold
        if r["witness_size_outlier"]:
            flagged.append((n, r.get("caller_option")))
    if flagged:
        log.warning("%d witness(es) far larger than the median of %d; these "
                    "may not build on a small machine: %s", len(flagged),
                    median, ", ".join("%s (%d settings)" % (c, n)
                                      for n, c in sorted(flagged, reverse=True)))
    return results


def load_queries(ctx: Context) -> list[dict]:
    return list(read_jsonl(ctx.queries_file))
