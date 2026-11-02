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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("benchmark")
    ap.add_argument("results")
    ap.add_argument("-o", "--output", help="write a Markdown table here")
    args = ap.parse_args()

    bench = json.load(open(args.benchmark))
    usable = [c for c in bench["cases"]
              if c.get("caller_option") and c.get("provider_options")
              and not c.get("excluded")]

    hits, misses = [], []
    for path in sorted(glob.glob(os.path.join(args.results, "score-*.json"))):
        d = json.load(open(path))
        for h in d.get("hits", []):
            hits.append(h)
        for m in d.get("misses", []):
            misses.append(m)

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

    excl = [c for c in bench["cases"] if c.get("excluded")]
    if excl:
        print("\nexcluded, not counted as misses (%d)" % len(excl))
        for reason, n in collections.Counter(
                classify(c["excluded"]) for c in excl).most_common():
            print("  %-52s %d" % (reason, n))

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
