"""S4 -- Reference conditions.

Estimate RC(c, s): when the reference to s is actually emitted.

  Input:  S2 presence table, S3 references, S1 symbol types.
  Output: s4/candidates.jsonl -- one candidate pair per line, with the RC rule
          chosen for it;  s4/guarded.jsonl -- pairs dropped as guarded.

Default rule (v1).  Assume the reference is emitted whenever the caller is
built and the provider is enabled at any level:

    RC(c, s) = available(c) and level(p) >= m

This models the common header pattern: a real declaration under
``IS_ENABLED(CONFIG_BAR)`` -- which is true for m -- and a ``static inline``
stub otherwise.  Under this rule the y/m case is satisfiable and the y/n and
m/n cases are not, which is the point: the stub protects the disabled case but
not the module case.  y/n and m/n therefore need evidence that the reference
survives with the provider off (`no_stub_evidence`), which v1 only has when a
build compiled the caller with the provider disabled.

Refinement.  A call site under ``IS_REACHABLE(CONFIG_BAR)`` or
``IS_BUILTIN(CONFIG_BAR)`` is already protected, so the pair is marked guarded
and dropped.  The scan does not need to be exact, because S6 confirms every
finding with a build.
"""

import os
import re
import time

import z3

from kreach.config import Context
from kreach.util import get_logger, human_time, read_jsonl, write_jsonl

log = get_logger("s4")

GUARD_MACROS = ("IS_REACHABLE", "IS_BUILTIN")
GUARD_RE = re.compile(r"\b(IS_REACHABLE|IS_BUILTIN)\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)")
ENABLED_RE = re.compile(r"\bIS_ENABLED\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)")

# How many lines on either side of a symbol mention count as "near".
GUARD_WINDOW = 40

RC_DEFAULT = "default"          # available(c) and available(p)
RC_NO_STUB = "no_stub_evidence" # available(c) only; reference survives p=n


class SourceIndex:
    """Lazily-read source files plus a symbol -> header index."""

    def __init__(self, ksrc: str):
        self.ksrc = ksrc
        self._text: dict[str, list[str]] = {}
        self._header_index: dict[str, list[str]] | None = None

    def lines(self, rel: str) -> list[str]:
        if rel in self._text:
            return self._text[rel]
        path = os.path.join(self.ksrc, rel)
        try:
            with open(path, errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        if len(self._text) > 4000:
            self._text.clear()
        self._text[rel] = lines
        return lines

    def source_for_object(self, obj: str) -> str | None:
        base = os.path.splitext(obj)[0]
        for ext in (".c", ".S", ".rs"):
            if os.path.exists(os.path.join(self.ksrc, base + ext)):
                return base + ext
        return None

    def build_header_index(self, symbols: set[str]) -> None:
        """Map each symbol of interest to the headers that mention it."""
        t0 = time.time()
        index: dict[str, list[str]] = {}
        nfiles = 0
        for dirpath, dirnames, filenames in os.walk(self.ksrc):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "Documentation", "tools", "scripts")]
            for fn in filenames:
                if not fn.endswith(".h"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    text = open(path, errors="replace").read()
                except OSError:
                    continue
                nfiles += 1
                if not any(m in text for m in GUARD_MACROS):
                    # Only headers that use a reachability macro can guard a
                    # call site, so the rest need no index entry.
                    continue
                rel = os.path.relpath(path, self.ksrc)
                for sym in symbols:
                    if sym in text:
                        index.setdefault(sym, []).append(rel)
        self._header_index = index
        log.info("header guard index: %d symbols appear in guarded headers "
                 "(%d headers scanned) in %s",
                 len(index), nfiles, human_time(time.time() - t0))

    def guarded_headers(self, sym: str) -> list[str]:
        return list((self._header_index or {}).get(sym, ()))


def _guard_near(lines: list[str], sym: str, window: int = GUARD_WINDOW):
    """Look for a reachability macro near a mention of `sym`.

    Returns (macro, CONFIG symbol, lineno) for the first hit, else None.
    """
    hits = [i for i, line in enumerate(lines) if sym in line]
    for i in hits:
        lo = max(0, i - window)
        hi = min(len(lines), i + window + 1)
        for j in range(lo, hi):
            m = GUARD_RE.search(lines[j])
            if m:
                return m.group(1), m.group(2), j + 1
    return None


def _pick_option(symbols: list[str], kconfig_types: dict, prefer_tristate=True) -> str | None:
    """Name the Kconfig option that controls an object."""
    cands = [s for s in symbols if kconfig_types.get(s) in ("bool", "tristate")]
    if not cands:
        cands = list(symbols)
    if not cands:
        return None
    if prefer_tristate:
        tri = [s for s in cands if kconfig_types.get(s) == "tristate"]
        if tri:
            cands = tri
    # The longest name is the most specific option in practice (CONFIG_SND_FOO
    # over CONFIG_SND), and ties are broken alphabetically for determinism.
    return sorted(cands, key=lambda s: (-len(s), s))[0]


def run_s4(ctx: Context, model, table, symbols: dict,
           max_candidates: int | None = None,
           feedback: dict | None = None) -> tuple[int, int]:
    """Build the candidate list.  Returns (candidates, guarded)."""
    t0 = time.time()
    references = symbols["references"]
    providers = symbols["providers"]
    weak = symbols.get("weak", {})
    module_map = symbols.get("module_map", {})
    composites = symbols.get("composites", {})
    symvers = symbols.get("symvers", {})
    kt = model.kconfig_types

    # A multi-part module is linked into one object whose undefined symbols are
    # the union of its parts', so attributing a reference to it loses whatever
    # guards the part that really made the call.  Where the parts were scanned
    # too, they are the precise callers and the composite is skipped.
    composite_callers = {obj for obj, parts in composites.items()
                         if obj in references
                         and any(p in references for p in parts)}
    composite_providers = {obj for obj, parts in composites.items()
                           if any(p in table for p in parts)}
    if composite_callers or composite_providers:
        log.info("composites: skipping %d linked module objects as callers and "
                 "%d as providers, in favour of their parts",
                 len(composite_callers), len(composite_providers))

    feedback = feedback or {}
    rejected_patterns = set(feedback.get("rejected_pairs", ()))

    src = SourceIndex(ctx.ksrc)
    wanted = {s for syms in references.values() for s in syms if s in providers}
    src.build_header_index(wanted)

    candidates: list[dict] = []
    guarded: list[dict] = []
    stats = {"no_provider": 0, "self_provided": 0, "weak": 0, "no_presence": 0,
             "multi_provider": 0, "guarded_site": 0, "guarded_header": 0,
             "rejected_feedback": 0, "composite_caller": 0,
             "provider_option_undefined": 0}

    for caller_obj in sorted(references):
        if caller_obj not in table:
            stats["no_presence"] += 1
            continue
        if caller_obj in composite_callers:
            stats["composite_caller"] += 1
            continue
        caller_src = src.source_for_object(caller_obj)
        caller_opt = _pick_option(table.own_symbols(caller_obj) or
                                  table.all_symbols(caller_obj), kt)
        for sym in sorted(references[caller_obj]):
            prov_objs = providers.get(sym)
            if not prov_objs:
                stats["no_provider"] += 1
                continue
            if sym in weak:
                # Weak symbols and __weak defaults are treated as always
                # provided; they are listed separately, not as candidates.
                stats["weak"] += 1
                continue
            prov_objs = [p for p in prov_objs if p != caller_obj]
            if not prov_objs:
                stats["self_provided"] += 1
                continue
            known = [p for p in prov_objs
                     if p in table and p not in composite_providers]
            if not known:
                stats["no_presence"] += 1
                continue
            if len(known) > 1:
                stats["multi_provider"] += 1
            for prov_obj in sorted(known):
                prov_opt = _pick_option(table.own_symbols(prov_obj) or
                                        table.all_symbols(prov_obj), kt)
                if not prov_opt or not caller_opt:
                    continue
                if caller_opt == prov_opt:
                    # Same option controls both; no configuration separates them.
                    continue
                if prov_opt in table.undefined:
                    # This architecture never declares the option guarding the
                    # provider, so that object is not built here at all and
                    # something else -- usually an arch/ object -- satisfies
                    # the reference.  lib/hweight.o under
                    # CONFIG_GENERIC_HWEIGHT is the example: x86 has its own
                    # arch/x86/lib/hweight.S.
                    stats["provider_option_undefined"] += 1
                    continue
                key = "%s|%s|%s" % (caller_opt, prov_opt, sym)
                if key in rejected_patterns:
                    stats["rejected_feedback"] += 1
                    continue

                # Refinement: drop pairs whose call site is already guarded.
                g = None
                if caller_src:
                    g = _guard_near(src.lines(caller_src), sym)
                where = caller_src
                if g is None:
                    for hdr in src.guarded_headers(sym):
                        g = _guard_near(src.lines(hdr), sym)
                        if g:
                            where = hdr
                            break
                    if g:
                        stats["guarded_header"] += 1
                else:
                    stats["guarded_site"] += 1
                if g:
                    macro, gsym, lineno = g
                    guarded.append({
                        "caller": caller_obj, "caller_option": caller_opt,
                        "provider": prov_obj, "provider_option": prov_opt,
                        "symbol": sym, "macro": macro, "guard_symbol": gsym,
                        "where": "%s:%d" % (where, lineno),
                    })
                    continue

                candidates.append({
                    "caller": caller_obj,
                    "caller_option": caller_opt,
                    "caller_source": caller_src,
                    "provider": prov_obj,
                    "provider_option": prov_opt,
                    "provider_module": module_map.get(prov_obj) or table.module_of(prov_obj),
                    "symbol": sym,
                    "exported": sym in symvers or sym in symbols.get("exports", {}),
                    "rc_rule": RC_DEFAULT,
                    "cases": ["y/m"],
                })
                if max_candidates and len(candidates) >= max_candidates:
                    log.warning("stopping at --max-candidates=%d", max_candidates)
                    break
            if max_candidates and len(candidates) >= max_candidates:
                break
        if max_candidates and len(candidates) >= max_candidates:
            break

    candidates = _merge_by_pair(candidates)
    write_jsonl(ctx.candidates_file, candidates)
    write_jsonl(ctx.guarded_file, guarded)
    ctx.write_meta({"s4": {
        "candidates": len(candidates),
        "guarded": len(guarded),
        "stats": stats,
        "seconds": round(time.time() - t0, 1),
    }})
    log.info("S4 complete: %d candidate pairs, %d guarded (%s) in %s",
             len(candidates), len(guarded),
             ", ".join("%s=%d" % kv for kv in sorted(stats.items()) if kv[1]),
             human_time(time.time() - t0))
    return len(candidates), len(guarded)


def _merge_by_pair(candidates: list[dict]) -> list[dict]:
    """One record per (caller, provider) pair, carrying all its symbols.

    A missing dependency breaks every symbol a caller uses from the provider,
    and the report wants them together.
    """
    merged: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["caller"], c["provider"])
        if key in merged:
            syms = merged[key].setdefault("symbols", [])
            if c["symbol"] not in syms:
                syms.append(c["symbol"])
            continue
        rec = dict(c)
        rec["symbols"] = [rec.pop("symbol")]
        merged[key] = rec
    out = list(merged.values())
    for rec in out:
        rec["symbols"] = sorted(rec["symbols"])
    out.sort(key=lambda r: (r["caller_option"] or "", r["provider_option"] or "",
                            r["caller"], r["provider"]))
    return out


def rc_formula(cand: dict, table, case: str):
    """RC(c, s) for a candidate under the rule recorded on it."""
    c_builtin, c_avail = table.conditions(cand["caller"])
    p_builtin, p_avail = table.conditions(cand["provider"])
    if cand.get("rc_rule", RC_DEFAULT) == RC_DEFAULT:
        rc = z3.And(c_avail, p_avail)
    else:
        rc = c_avail
    return rc, (c_builtin, c_avail, p_builtin, p_avail)


def load_candidates(ctx: Context) -> list[dict]:
    return list(read_jsonl(ctx.candidates_file))


# Verdicts that mean "S6 looked and this pair does not fail".  A build that
# was interrupted or fell over before it could link says nothing about the
# pair, and must not be fed back as a rejection -- that would drop the pair
# from every later run on the strength of a machine hiccup.
S6_REJECTIONS = frozenset({
    "rejected_build_ok",
    "rejected_other_undefined",
    "rejected_other_error",
    "skipped_witness_defanged",
    "skipped_reference_not_emitted",
})


def feedback_from_s6(ctx: Context) -> dict:
    """Pairs S6 rejected, so S4 can skip them on the next run.

    This is the pipeline's one feedback loop: candidates that S6 rejects feed
    back into the rules of S4.
    """
    rejected = []
    for rec in read_jsonl(ctx.confirm_file):
        if rec.get("status") in S6_REJECTIONS:
            for sym in rec.get("symbols", ()):
                rejected.append("%s|%s|%s" % (rec.get("caller_option"),
                                              rec.get("provider_option"), sym))
    return {"rejected_pairs": rejected}
