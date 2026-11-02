# SPDX-License-Identifier: GPL-2.0-or-later
"""Historical-recall benchmark.

Build a benchmark from git history: commits whose message mentions
``undefined reference`` or ``IS_REACHABLE`` and that change only Kconfig
lines.  For each, check out the parent commit and run the tool; a hit means
the tool reports the same caller/provider pair the fix addressed.

This module does the first half -- mining the benchmark and extracting the
caller/provider pair each fix addressed -- and scores a findings file against
it.  Running the tool on every parent commit is driven from outside (one
work directory per commit), because each parent needs its own S1..S6.
"""

import re
import time

from kreach.config import Context
from kreach.util import get_logger, human_time, read_jsonl, run

log = get_logger("bench")

GREP_PATTERNS = ("undefined reference", "IS_REACHABLE")

# Kconfig stanza parsing over a diff
RE_CONFIG_STANZA = re.compile(r"^[+\- ]?\s*(?:menu)?config\s+([A-Z0-9_]+)\s*$")
# git puts the enclosing stanza after the hunk header:
#   @@ -10,6 +10,7 @@ config SND_FOO
RE_HUNK_STANZA = re.compile(r"^@@[^@]*@@\s*(?:menu)?config\s+([A-Z0-9_]+)")
RE_ADDED_DEPENDS = re.compile(r"^\+\s*depends on\s+(.+?)\s*$")
RE_ADDED_IS_REACHABLE = re.compile(r"^\+.*\bIS_REACHABLE\s*\(\s*(CONFIG_[A-Z0-9_]+)\s*\)")
RE_UNDEF_SYM = re.compile(r"undefined reference to [`'\"]([A-Za-z_][A-Za-z0-9_.]*)")
RE_MODPOST_SYM = re.compile(r"[\"']([A-Za-z_][A-Za-z0-9_.]*)[\"'][^\n]*undefined")
RE_OPTION = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")

KCONFIG_FILE_RE = re.compile(r"(^|/)(Kconfig|Kconfig\.[A-Za-z0-9_.\-]+)$")


def _git(ctx: Context, args: list[str], check=True) -> str:
    return run(["git"] + args, cwd=ctx.ksrc, check=check).stdout


def mine_commits(ctx: Context, since: str = "v6.0", limit: int = 50) -> list[dict]:
    """Find candidate fix commits: Kconfig-only changes with a link error."""
    seen: dict[str, dict] = {}
    for pattern in GREP_PATTERNS:
        out = _git(ctx, [
            "log", "--no-merges", "-i", "--grep", pattern,
            "--format=%H%x1f%P%x1f%s%x1f%ad%x1e", "--date=short",
            "%s..HEAD" % since if since else "HEAD",
        ], check=False)
        for entry in out.split("\x1e"):
            entry = entry.strip("\n")
            if not entry:
                continue
            parts = entry.split("\x1f")
            if len(parts) < 4:
                continue
            sha, parents, subject, date = parts[0], parts[1], parts[2], parts[3]
            if sha in seen:
                seen[sha]["matched"].append(pattern)
                continue
            seen[sha] = {
                "commit": sha,
                "parent": parents.split()[0] if parents else None,
                "subject": subject,
                "date": date,
                "matched": [pattern],
            }
    log.info("message grep matched %d commits since %s", len(seen), since)

    kconfig_only: list[dict] = []
    for sha, rec in seen.items():
        files = _git(ctx, ["show", "--name-only", "--format=", sha], check=False).split()
        files = [f for f in files if f]
        if not files:
            continue
        rec["files"] = files
        if all(KCONFIG_FILE_RE.search(f) for f in files):
            kconfig_only.append(rec)
    kconfig_only.sort(key=lambda r: r["date"], reverse=True)
    log.info("%d of those change only Kconfig files", len(kconfig_only))
    return kconfig_only[:limit]


def extract_pair(ctx: Context, rec: dict) -> dict:
    """What caller/provider pair did this fix address?"""
    diff = _git(ctx, ["show", "--format=%B", rec["commit"]], check=False)
    body, _, _ = diff.partition("\ndiff --git")
    symbols = sorted(set(RE_UNDEF_SYM.findall(body)) |
                     set(RE_MODPOST_SYM.findall(body)))

    caller_option = None
    providers: list[str] = []
    patterns: list[str] = []
    current = None
    for line in diff.splitlines():
        m = RE_CONFIG_STANZA.match(line) or RE_HUNK_STANZA.match(line)
        if m:
            current = "CONFIG_" + m.group(1)
            continue
        m = RE_ADDED_DEPENDS.match(line)
        if m:
            expr = m.group(1)
            caller_option = caller_option or current
            opts = ["CONFIG_" + o for o in RE_OPTION.findall(expr)
                    if o not in ("DEPENDS", "ON")]
            providers.extend(opts)
            patterns.append("depends on %s" % expr)
            if "||" in expr and "!" in expr:
                patterns[-1] = "depends on X || !X"
            continue
        m = RE_ADDED_IS_REACHABLE.match(line)
        if m:
            providers.append(m.group(1))
            patterns.append("IS_REACHABLE")

    rec = dict(rec)
    rec.update({
        "symbols": symbols,
        "caller_option": caller_option,
        "provider_options": sorted(set(providers)),
        "fix_patterns": sorted(set(patterns)),
    })
    return rec


def build_benchmark(ctx: Context, limit: int = 50, since: str = "v6.0") -> dict:
    t0 = time.time()
    commits = mine_commits(ctx, since=since, limit=limit)
    cases = [extract_pair(ctx, rec) for rec in commits]
    usable = [c for c in cases
              if c["caller_option"] and c["provider_options"]]
    result = {
        "since": since,
        "candidates": len(commits),
        "cases": cases,
        "usable": len(usable),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("benchmark: %d commits, %d with an extractable caller/provider "
             "pair (%s)", len(cases), len(usable), human_time(time.time() - t0))
    return result


def same_commit(a: str | None, b: str | None) -> bool:
    """Whether two revisions name the same commit, either abbreviated.

    The benchmark stores 40 characters because that is what ``git log``
    gives it; a person, and tools/bench_replicate.sh after them, passes the
    short form.  Comparing the two as strings silently matches nothing.
    """
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def score(benchmark: dict, findings_file: str, commit: str | None = None) -> dict:
    """Did the tool report the pair a benchmark fix addressed?"""
    findings = list(read_jsonl(findings_file))
    reported = {(f["caller"]["option"], f["provider"]["option"]) for f in findings}
    reported_callers = {f["caller"]["option"] for f in findings}

    if commit and not any(same_commit(c.get("commit"), commit)
                          for c in benchmark["cases"]):
        log.warning("no benchmark case for %s; the mined set has %d cases, so "
                    "either it was mined from a different range or the "
                    "revision is wrong", commit, len(benchmark["cases"]))

    hits, misses = [], []
    for case in benchmark["cases"]:
        if not case.get("caller_option") or not case.get("provider_options"):
            continue
        if commit and not same_commit(case["commit"], commit):
            continue
        want = {(case["caller_option"], p) for p in case["provider_options"]}
        if want & reported:
            hits.append({"commit": case["commit"], "subject": case["subject"],
                         "pair": sorted(want & reported)[0]})
        elif case["caller_option"] in reported_callers:
            misses.append({"commit": case["commit"], "subject": case["subject"],
                           "reason": "caller reported, different provider"})
        else:
            misses.append({"commit": case["commit"], "subject": case["subject"],
                           "reason": "not reported"})
    total = len(hits) + len(misses)
    return {
        "hits": hits,
        "misses": misses,
        "recall": round(len(hits) / total, 3) if total else None,
        "checked": total,
    }
