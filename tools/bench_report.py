# SPDX-License-Identifier: GPL-2.0-or-later
"""Aggregate the historical-recall sweep.

  tools/bench_report.py <benchmark.json> <results/benchmark> [-o table.md]

One verdict per case lives in its own score file, written as the sweep goes,
so the aggregate is just a sum -- but two things have to be got right for it
to mean anything.

Excluded cases are not misses: a pair that cannot be a y/m instance at the
parent says something about the mining or the architecture, not about the
checker, so it is counted apart and the recall denominator leaves it out.
And a verdict recorded before a harness fix is not evidence about kreach;
those live in superseded/ and are ignored here by construction, since only
score-*.json at the top level is read.
"""

import argparse
import collections
import glob
import json
import os
import sys


def classify(reason: str) -> str:
    if "already depends on" in reason:
        return "caller already depends on the provider"
    if "can be a module" in reason:
        return "provider is a bool: the y/n case, out of scope for v1"
    if "another architecture" in reason:
        return "caller belongs to another architecture"
    return reason[:60]


def triage(results: str, bench: dict, misses: list[dict]) -> dict:
    """Group misses by what the saved per-case diagnostics say stopped them.

    Only the cases still counted as misses are grouped: a case the miner has
    since excluded is not a miss and must not appear here.
    """
    cases = {c["commit"][:12]: c for c in bench["cases"]}
    out = collections.defaultdict(list)
    for m in misses:
        sha = m["commit"][:12]
        case = cases.get(sha, {})
        d = os.path.join(results, "diag-" + sha)
        qf, rf = os.path.join(d, "queries.jsonl"), os.path.join(d, "results.jsonl")
        if not os.path.exists(qf):
            out["no diagnostics saved for this case"].append((sha, m["subject"]))
            continue
        qs = [json.loads(l) for l in open(qf) if l.strip()]
        rs = [json.loads(l) for l in open(rf) if l.strip()] if os.path.exists(rf) else []
        provs = set(case.get("provider_options") or [])
        rst = collections.Counter(r.get("status") for r in rs)
        on_target = [q for q in qs if q.get("provider_option") in provs]
        if not qs:
            cause = "no candidate pair was formed at all"
        elif not on_target:
            cause = "candidates formed, none against a provider the fix names"
        elif rst.get("confirmed"):
            cause = "kreach confirmed a finding the scorer did not match"
        elif rst.get("skipped_reference_not_emitted"):
            cause = "witness built, but the caller does not emit the reference"
        elif rst.get("rejected_build_ok"):
            cause = "witness built and linked cleanly: a false candidate"
        elif any(q["status"] == "witness_rejected" for q in on_target):
            cause = "satisfiable, but Kconfig rejected the witness (the S1 gap)"
        elif all(q["status"] == "unsat" for q in on_target):
            cause = "K says the y/m case is unreachable"
        else:
            cause = "build failed for an unrelated reason"
        out[cause].append((sha, m["subject"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("benchmark")
    ap.add_argument("results")
    ap.add_argument("-o", "--output", help="write a Markdown table here")
    ap.add_argument("--triage", action="store_true",
                    help="group the misses by what the saved diagnostics say "
                         "stopped each one")
    args = ap.parse_args()

    bench = json.load(open(args.benchmark))
    usable = [c for c in bench["cases"]
              if c.get("caller_option") and c.get("provider_options")
              and not c.get("excluded")]

    # A verdict for a case the miner has since excluded is stale: the rules
    # grew as misses were diagnosed, and a case ruled out afterwards must
    # not keep counting against recall.
    excluded_shas = {c["commit"][:12] for c in bench["cases"] if c.get("excluded")}
    hits, misses, stale = [], [], []
    for path in sorted(glob.glob(os.path.join(args.results, "score-*.json"))):
        sha = os.path.basename(path)[len("score-"):-len(".json")]
        if sha in excluded_shas:
            stale.append(sha)
            continue
        d = json.load(open(path))
        hits.extend(d.get("hits", []))
        misses.extend(d.get("misses", []))

    scored = len(hits) + len(misses)
    print("usable cases      %d" % len(usable))
    print("scored so far     %d" % scored)
    print("  hits            %d" % len(hits))
    print("  misses          %d" % len(misses))
    print("recall over scored %s" % (
        "%.2f" % (len(hits) / scored) if scored else "n/a"))
    print("still to run      %d" % (len(usable) - scored))

    if hits:
        print("\nhits")
        for h in sorted(hits, key=lambda h: h["commit"]):
            print("  %s %-54s [%s]" % (h["commit"][:12], h["subject"][:54],
                                       h.get("matched", "pair")))
    if misses:
        print("\nmisses")
        for m in sorted(misses, key=lambda m: m["commit"]):
            print("  %s %-54s %s" % (m["commit"][:12], m["subject"][:54],
                                     m.get("reason", "?")))

    if stale:
        print("\nverdicts ignored, the case is now excluded (%d): %s"
              % (len(stale), ", ".join(sorted(stale))))

    excl = [c for c in bench["cases"] if c.get("excluded")]
    if excl:
        print("\nexcluded, not counted as misses (%d)" % len(excl))
        for reason, n in collections.Counter(
                classify(c["excluded"]) for c in excl).most_common():
            print("  %-52s %d" % (reason, n))

    if args.triage:
        print("\nmisses by cause")
        for cause, items in sorted(triage(args.results, bench, misses).items(),
                                   key=lambda kv: -len(kv[1])):
            print("  %s (%d)" % (cause, len(items)))
            for sha, subj in items:
                print("      %s %s" % (sha, subj[:58]))

    if args.output:
        rows = ["| case | subject | result |", "|---|---|---|"]
        for h in sorted(hits, key=lambda h: h["commit"]):
            rows.append("| `%s` | %s | **hit** (%s) |" % (
                h["commit"][:12], h["subject"], h.get("matched", "pair")))
        for m in sorted(misses, key=lambda m: m["commit"]):
            rows.append("| `%s` | %s | miss — %s |" % (
                m["commit"][:12], m["subject"], m.get("reason", "?")))
        with open(args.output, "w") as fh:
            fh.write("\n".join(rows) + "\n")
        print("\nwrote %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
