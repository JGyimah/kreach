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

# Reading one stanza out of a Kconfig file as it stood at some revision.
RE_STANZA = re.compile(r"^\s*(?:menu)?config\s+([A-Z0-9_]+)\s*$")
RE_STANZA_TYPE = re.compile(r"^\s+(bool|tristate|string|int|hex)\b")
RE_STANZA_DEPENDS = re.compile(r"^\s+depends on\s+(.+?)\s*$")


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


def and_terms(expr: str) -> list[str]:
    """The top-level ``&&`` terms of a depends-on expression."""
    terms, depth, cur = [], 0, ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr[i:i + 2] == "&&":
            terms.append(cur)
            cur = ""
            i += 2
            continue
        cur += c
        i += 1
    terms.append(cur)
    return [t.strip() for t in terms if t.strip()]


def kconfig_stanza(ctx: Context, rev: str, paths: list[str],
                   option: str) -> tuple[str | None, list[str]]:
    """The type and ``depends on`` lines of one option, at one revision.

    Read from the Kconfig files the fix touched, since that is where the
    option it is about is declared.
    """
    name = option[len("CONFIG_"):] if option.startswith("CONFIG_") else option
    for path in paths:
        if not KCONFIG_FILE_RE.search(path):
            continue
        text = _git(ctx, ["show", "%s:%s" % (rev, path)], check=False)
        if not text:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = RE_STANZA.match(line)
            if not m or m.group(1) != name:
                continue
            typ: str | None = None
            deps: list[str] = []
            for body in lines[i + 1:]:
                # A stanza ends at the next unindented, non-blank line.
                if body.strip() and not body[0].isspace():
                    break
                t = RE_STANZA_TYPE.match(body)
                if t and typ is None:
                    typ = t.group(1)
                d = RE_STANZA_DEPENDS.match(body)
                if d:
                    deps.append(d.group(1))
            return typ, deps
    return None, []


def declaring_paths(ctx: Context, rev: str, name: str,
                    cache: dict | None = None) -> list[str]:
    """Kconfig files that declare an option, at one revision."""
    key = (rev, name)
    if cache is not None and key in cache:
        return cache[key]
    out = _git(ctx, ["grep", "-lE", r"^(menu)?config %s$" % name, rev,
                     "--", "*Kconfig*"], check=False)
    paths = [l.split(":", 1)[1] for l in out.splitlines() if ":" in l]
    if cache is not None:
        cache[key] = paths
    return paths


def symbol_type(ctx: Context, rev: str, option: str,
                cache: dict | None = None) -> str | None:
    """An option's declared type at one revision, found tree-wide."""
    name = option[len("CONFIG_"):] if option.startswith("CONFIG_") else option
    paths = declaring_paths(ctx, rev, name, cache)
    if not paths:
        return None
    typ, _ = kconfig_stanza(ctx, rev, paths, option)
    return typ


def provider_can_be_a_module(ctx: Context, rev: str, provider_option: str,
                             cache: dict | None = None) -> bool:
    """Whether the provider can hold the ``m`` the y/m case needs.

    Only a tristate has an ``m``.  A provider declared ``bool`` -- a menu
    gate such as NET_VENDOR_FREESCALE, say -- cannot be a module at all, so
    no configuration satisfies ``level(provider) == m`` and the pair is not a
    y/m instance however the fix was written.  An option that is not declared
    anywhere is left alone rather than guessed at.
    """
    typ = symbol_type(ctx, rev, provider_option, cache)
    return typ != "bool"


def caller_buildable_on(ctx: Context, rev: str, srcarch: str,
                        caller_option: str, depends: list[str],
                        cache: dict | None = None) -> bool:
    """Whether this architecture can build the caller at all.

    A driver gated on another architecture's symbol, with no COMPILE_TEST
    escape, is unselectable here: S3 never compiles it, so no reference of
    its is ever seen and the case cannot be found whatever the checker does.
    A symbol counts as another architecture's when every Kconfig file
    declaring it sits under ``arch/`` and none under ``arch/<srcarch>/``.
    """
    if any("COMPILE_TEST" in expr for expr in depends):
        return True
    for expr in depends:
        for term in and_terms(expr):
            if not re.fullmatch(r"[A-Z0-9_]+", term):
                continue            # not a bare requirement
            paths = declaring_paths(ctx, rev, term, cache)
            if not paths:
                continue
            if all(p.startswith("arch/") for p in paths) and \
               not any(p.startswith("arch/%s/" % srcarch) for p in paths):
                return False
    return True


def dependency_caps_level(caller_type: str | None, depends: list[str],
                          provider_option: str) -> bool:
    """Whether the caller's own ``depends on`` already forbids the y/m case.

    A tristate with ``depends on P`` cannot be ``y`` while P is ``m``:
    Kconfig caps a symbol at the level of what it depends on, which is what
    R6 encodes.  A pair of that shape is not a y/m instance, so scoring it
    measures the miner rather than the checker.

    A bool caller is the opposite.  It has no ``m`` level, so ``depends on P``
    happily leaves it ``y`` with P at ``m`` -- that is the hazard, not a
    protection, and such a pair must be kept.

    Only a bare conjunct counts.  ``depends on P=y`` is an exact-level test
    rather than a cap, and a term inside ``||`` is not required at all.
    """
    if caller_type != "tristate":
        return False
    name = (provider_option[len("CONFIG_"):]
            if provider_option.startswith("CONFIG_") else provider_option)
    for expr in depends:
        for term in and_terms(expr):
            if term == name:
                return True
    return False


def extract_pair(ctx: Context, rec: dict, cache: dict | None = None) -> dict:
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
    providers = sorted(set(providers))
    rec.update({
        "symbols": symbols,
        "caller_option": caller_option,
        "provider_options": providers,
        "fix_patterns": sorted(set(patterns)),
    })

    # Three ways the extracted pair cannot be a y/m instance at the parent.
    # Counting any of them as a miss would measure the mining, or the
    # architecture, rather than the checker.
    caller_type, depends = (None, [])
    if caller_option and rec.get("parent"):
        caller_type, depends = kconfig_stanza(ctx, rec["parent"],
                                              rec.get("files") or [],
                                              caller_option)
    rec["caller_type"] = caller_type
    rec["caller_depends"] = depends

    capped = [p for p in providers
              if dependency_caps_level(caller_type, depends, p)]
    rec["capped_providers"] = capped

    parent = rec.get("parent")
    modular = providers
    if parent and providers:
        modular = [p for p in providers
                   if provider_can_be_a_module(ctx, parent, p, cache)]
        rec["non_modular_providers"] = [p for p in providers if p not in modular]

    buildable = True
    if parent and caller_option:
        buildable = caller_buildable_on(ctx, parent, ctx.srcarch, caller_option,
                                        depends, cache)
    rec["caller_buildable"] = buildable

    if providers and len(capped) == len(providers):
        rec["excluded"] = (
            "%s is a tristate and already depends on %s at the parent, so it "
            "cannot be y while that is m" % (
                caller_option, ", ".join(capped)))
    elif providers and not modular:
        rec["excluded"] = (
            "no provider the fix names can be a module (%s is bool), so the "
            "bug is the y/n case, which v1 does not check" % ", ".join(providers))
    elif not buildable:
        rec["excluded"] = (
            "%s is gated on another architecture with no COMPILE_TEST, so "
            "ARCH=%s cannot build it (depends on %s)" % (
                caller_option, ctx.arch, "; ".join(depends) or "?"))
    return rec


def build_benchmark(ctx: Context, limit: int = 50, since: str = "v6.0") -> dict:
    t0 = time.time()
    commits = mine_commits(ctx, since=since, limit=limit)
    cache: dict = {}
    cases = [extract_pair(ctx, rec, cache) for rec in commits]
    extractable = [c for c in cases
                   if c["caller_option"] and c["provider_options"]]
    excluded = [c for c in extractable if c.get("excluded")]
    usable = [c for c in extractable if not c.get("excluded")]
    result = {
        "since": since,
        "candidates": len(commits),
        "cases": cases,
        "usable": len(usable),
        "excluded": len(excluded),
        "seconds": round(time.time() - t0, 1),
    }
    log.info("benchmark: %d commits, %d with an extractable caller/provider "
             "pair, %d of those excluded as not a y/m instance -> %d usable "
             "(%s)", len(cases), len(extractable), len(excluded), len(usable),
             human_time(time.time() - t0))
    for c in excluded:
        log.debug("excluded %s: %s", c["commit"][:12], c["excluded"])
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


def by_symbol_and_provider(case: dict, findings: list[dict]) -> list[dict]:
    """Findings naming a symbol of this case, from a provider it names."""
    syms = set(case.get("symbols") or [])
    provs = set(case.get("provider_options") or [])
    if not syms or not provs:
        return []
    return [f for f in findings
            if f["provider"]["option"] in provs
            and syms & set(f.get("symbols") or [])]


def symbol_sources(ctx: Context, rev: str, symbols: list[str],
                   cache: dict | None = None) -> set[str]:
    """Source files that mention any of these symbols, at one revision."""
    out: set[str] = set()
    for sym in symbols:
        key = (rev, sym)
        if cache is not None and key in cache:
            out |= cache[key]
            continue
        got = set()
        # -w, not a substring match: a symbol `foo` otherwise matches every
        # file mentioning `foo_bar`, which loosens the source check this
        # exists to tighten.
        for line in _git(ctx, ["grep", "-lw", sym, rev], check=False).splitlines():
            if ":" in line:
                got.add(line.split(":", 1)[1])
        if cache is not None:
            cache[key] = got
        out |= got
    return out


def score(benchmark: dict, findings_file: str, commit: str | None = None,
          ctx: Context | None = None) -> dict:
    """Did the tool report the bug a benchmark fix addressed?

    The obvious test is whether the reported (caller option, provider option)
    pair is the mined one, and it is the one to prefer.  But the two sides do
    not always name the caller the same way.  A fix edits the stanza a human
    thinks of as the driver -- often a bool, such as MLX5_MACSEC -- while
    kreach names the option whose *level* puts the object in vmlinux, the
    tristate on the ``obj-`` line.  Both are correct about different things,
    and an exact-pair test scores the same bug as a miss.

    So a second form of hit is allowed: the same symbol, from one of the
    providers the fix names, reported against a caller that is one of the
    files referencing that symbol at the parent.  The symbol is what makes
    this tight -- it pins the finding to the bug rather than to the class --
    and `ctx` is what makes the last clause checkable.  Without `ctx` only
    exact pairs count, which is the conservative reading.
    """
    findings = list(read_jsonl(findings_file))
    reported = {(f["caller"]["option"], f["provider"]["option"]) for f in findings}
    reported_callers = {f["caller"]["option"] for f in findings}
    sources: dict = {}

    if commit and not any(same_commit(c.get("commit"), commit)
                          for c in benchmark["cases"]):
        log.warning("no benchmark case for %s; the mined set has %d cases, so "
                    "either it was mined from a different range or the "
                    "revision is wrong", commit, len(benchmark["cases"]))

    hits, misses, excluded = [], [], []
    for case in benchmark["cases"]:
        if not case.get("caller_option") or not case.get("provider_options"):
            continue
        if commit and not same_commit(case["commit"], commit):
            continue
        if case.get("excluded"):
            excluded.append({"commit": case["commit"], "subject": case["subject"],
                             "reason": case["excluded"]})
            continue
        want = {(case["caller_option"], p) for p in case["provider_options"]}
        if want & reported:
            hits.append({"commit": case["commit"], "subject": case["subject"],
                         "pair": sorted(want & reported)[0], "matched": "pair"})
            continue

        near = by_symbol_and_provider(case, findings)
        if near and ctx and case.get("parent"):
            refs = symbol_sources(ctx, case["parent"], case.get("symbols") or [],
                                  sources)
            near = [f for f in near if (f["caller"].get("source") or "") in refs]
        elif near and not ctx:
            near = []           # unverifiable without a tree; do not credit it
        if near:
            f = near[0]
            hits.append({
                "commit": case["commit"], "subject": case["subject"],
                "pair": [f["caller"]["option"], f["provider"]["option"]],
                "matched": "symbol and provider",
                "note": "kreach names the caller %s where the fix edits %s; "
                        "same symbol %s from %s, and the caller is %s" % (
                            f["caller"]["option"], case["caller_option"],
                            (f.get("symbols") or ["?"])[0],
                            f["provider"]["option"], f["caller"].get("source")),
            })
            continue

        if case["caller_option"] in reported_callers:
            misses.append({"commit": case["commit"], "subject": case["subject"],
                           "reason": "caller reported, different provider"})
        else:
            misses.append({"commit": case["commit"], "subject": case["subject"],
                           "reason": "not reported"})
    total = len(hits) + len(misses)
    return {
        "hits": hits,
        "misses": misses,
        # Kept separate rather than counted as misses: these are cases whose
        # extracted pair cannot be a y/m instance at the parent.
        "excluded": excluded,
        "recall": round(len(hits) / total, 3) if total else None,
        "checked": total,
    }
