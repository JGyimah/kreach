"""S7 -- Report and fix suggestion.

Turn confirmed findings into material for a patch.

  Group by the Kconfig symbol of the caller, so one report becomes one patch.
  Each finding also carries what the patch conventions ask for: a ``Fixes:``
  tag for the commit that introduced the reference, and the maintainers
  ``scripts/get_maintainer.pl`` names for the caller's file.
  Suggest a fix from the provider's role:
    * ``depends on BAR``          if every caller path needs it
    * ``depends on BAR || !BAR``  if the code has a stub for BAR=n
    * ``IS_REACHABLE()``          only as a last resort, flagged as such
  Never suggest ``select``.

Output: s7/findings.jsonl (one record per finding, holding everything needed
to write and test the patch with no rerun of the tool), s7/findings.txt (the
reproduction section for a commit message), s7/summary.json.
"""

import os
import re
import time

from kreach.config import Context
from kreach.util import get_logger, read_jsonl, run, write_json, write_jsonl

log = get_logger("s7")

FIX_DEPENDS = "depends on"
FIX_DEPENDS_OR_NOT = "depends on ... || !..."
FIX_IS_REACHABLE = "IS_REACHABLE()"

STUB_WINDOW = 25
IS_ENABLED_RE = re.compile(r"\bIS_ENABLED\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)")
STATIC_INLINE_RE = re.compile(r"\bstatic\s+inline\b")


def fixes_tag(ksrc: str, source: str | None, symbols: list[str],
              cache: dict | None = None) -> dict | None:
    """The ``Fixes:`` tag for the commit that introduced the reference.

    First choice is ``git log -S<symbol>`` over the caller's file, which finds
    the commit that added the call when the symbol appears in the source.
    Often it does not: ``sound/soc/fsl/fsl_xcvr.c`` calls ``bitrev32()`` and
    never writes ``byte_rev_table``, which is what the linker is missing.  The
    fallback is the commit that added the file, which is the right target for
    a driver that has been missing the dependency since it landed -- and the
    record says which of the two was used, because the second is a starting
    point rather than an answer.
    """
    cache = cache if cache is not None else {}
    if not source:
        return None
    key = (source, tuple(sorted(symbols)))
    if key in cache:
        return cache[key]

    def git(args):
        try:
            out = run(["git", "-C", ksrc] + args, check=False, timeout=120)
        except Exception:
            return ""
        return out.stdout if out.returncode == 0 else ""

    found = None
    for sym in symbols:
        line = git(["log", "-S", sym, "--format=%H%x1f%s", "--reverse",
                    "--", source]).strip().split("\n")[0]
        if line and "\x1f" in line:
            sha, _, subject = line.partition("\x1f")
            found = {"commit": sha[:12], "subject": subject,
                     "how": "git log -S%s on %s" % (sym, source)}
            break
    if found is None:
        line = git(["log", "--diff-filter=A", "--format=%H%x1f%s",
                    "--", source]).strip().split("\n")[0]
        if line and "\x1f" in line:
            sha, _, subject = line.partition("\x1f")
            found = {"commit": sha[:12], "subject": subject,
                     "how": "commit that added %s; the symbol does not appear "
                            "in the source, so check this one" % source}
    if found:
        found["tag"] = 'Fixes: %s ("%s")' % (found["commit"], found["subject"])
    cache[key] = found
    return found


def maintainers(ksrc: str, source: str | None, cache: dict | None = None,
                limit: int = 6) -> list[str]:
    """Who to send the patch to, from scripts/get_maintainer.pl."""
    cache = cache if cache is not None else {}
    if not source:
        return []
    if source in cache:
        return cache[source]
    script = os.path.join(ksrc, "scripts", "get_maintainer.pl")
    out: list[str] = []
    if os.path.exists(script):
        try:
            proc = run(["perl", script, "--nogit", "--nogit-fallback",
                        "--no-rolestats", "-f", source],
                       cwd=ksrc, check=False, timeout=180)
            if proc.returncode == 0:
                out = [l.strip() for l in proc.stdout.splitlines() if l.strip()][:limit]
        except Exception:
            out = []
    cache[source] = out
    return out


def _option_tail(option: str) -> str:
    """CONFIG_BAR -> BAR, the name Kconfig files use."""
    return option[len("CONFIG_"):] if option.startswith("CONFIG_") else option


def _find_declaring_headers(ksrc: str, symbols: list[str], cache: dict) -> list[str]:
    """Headers that mention any of `symbols` (cached across findings)."""
    key = tuple(sorted(symbols))
    if key in cache:
        return cache[key]
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ksrc, "include")):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            if not fn.endswith(".h"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                text = open(path, errors="replace").read()
            except OSError:
                continue
            if any(s in text for s in symbols):
                hits.append(os.path.relpath(path, ksrc))
    cache[key] = hits
    return hits


def detect_stub(ksrc: str, symbols: list[str], provider_option: str,
                extra_headers=(), cache: dict | None = None) -> dict:
    """Does the code already have a stub for provider_option=n?

    The pattern we look for is the usual one: a real declaration under
    ``IS_ENABLED(CONFIG_BAR)`` with a ``static inline`` stub in the #else
    branch.  Its presence is what makes ``depends on BAR || !BAR`` the right
    fix rather than ``depends on BAR``.
    """
    cache = cache if cache is not None else {}
    headers = list(extra_headers) + _find_declaring_headers(ksrc, symbols, cache)
    for hdr in headers:
        try:
            lines = open(os.path.join(ksrc, hdr), errors="replace").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not any(s in line for s in symbols):
                continue
            lo, hi = max(0, i - STUB_WINDOW), min(len(lines), i + STUB_WINDOW + 1)
            window = "\n".join(lines[lo:hi])
            enabled = IS_ENABLED_RE.findall(window)
            has_inline = bool(STATIC_INLINE_RE.search(window))
            if has_inline and provider_option in enabled:
                return {"stub": True, "header": "%s:%d" % (hdr, i + 1),
                        "reason": "static inline stub under IS_ENABLED(%s)" % provider_option}
            if has_inline and enabled:
                return {"stub": True, "header": "%s:%d" % (hdr, i + 1),
                        "reason": "static inline stub under %s" % ", ".join(sorted(set(enabled)))}
    return {"stub": False, "header": None, "reason": "no static inline stub found"}


def suggest_fix(rec: dict, ksrc: str, cache: dict | None = None,
                visible: set[str] | None = None) -> dict:
    """Pick the standard Kconfig fix for a confirmed finding.

    `visible` is the set of Kconfig symbols that have a prompt.  When the
    provider has none -- a library tristate such as ``BITREVERSE``, which
    exists only to be selected -- ``depends on`` is still the fix this tool
    reports, because the spec rules ``select`` out, but it is not on its own
    actionable: nothing would ever turn the provider on, so the caller would
    become unselectable.  That gets said out loud in `caveat` rather than
    left for the maintainer to discover.
    """
    caller = _option_tail(rec["caller_option"])
    provider = _option_tail(rec["provider_option"])
    case = rec["case"]
    stub = detect_stub(ksrc, rec["symbols"], rec["provider_option"],
                       extra_headers=(), cache=cache)
    caveat = None
    if visible is not None and rec["provider_option"] not in visible:
        caveat = ("%s has no prompt, so it is only ever enabled by a select; "
                  "'depends on %s' would leave %s unselectable.  This one "
                  "needs a maintainer decision, not the standard fix."
                  % (provider, provider, caller))

    if case == "y/m" and stub["stub"]:
        # The caller copes with BAR=n but not with BAR=m while itself y.
        return {
            "pattern": FIX_DEPENDS_OR_NOT,
            "kconfig": "depends on %s || !%s" % (provider, provider),
            "where": "config %s" % caller,
            "rationale": "%s already has a stub for %s=n (%s), so it only needs "
                         "to be kept out of vmlinux when %s is a module."
                         % (caller, provider, stub["reason"], provider),
            "stub": stub,
            "last_resort": False,
            "caveat": caveat,
        }
    if case in ("y/n", "m/n") or not stub["stub"]:
        return {
            "pattern": FIX_DEPENDS,
            "kconfig": "depends on %s" % provider,
            "where": "config %s" % caller,
            "rationale": "%s has no stub for %s=n (%s), so every caller path "
                         "needs %s present." % (caller, provider, stub["reason"], provider),
            "stub": stub,
            "last_resort": False,
            "caveat": caveat,
        }
    return {
        "pattern": FIX_IS_REACHABLE,
        "kconfig": "use IS_REACHABLE(%s) at the call site" % rec["provider_option"],
        "where": rec.get("caller_source") or rec["caller"],
        "rationale": "No Kconfig dependency is appropriate: %s must stay "
                     "buildable with %s as a module.  Flagged as a last resort."
                     % (caller, provider),
        "stub": stub,
        "last_resort": True,
        "caveat": caveat,
    }


def module_name(rec: dict) -> str | None:
    """The .ko the provider ends up in, for the record's provider field.

    S3 reads the exact object-to-module mapping out of a build's ``.mod``
    files, but only for the subtree it built: a provider outside it --
    ``lib/bitrev.o`` for a caller in ``sound/`` -- has no entry.  A single-file
    module is named after its object, which is how Kbuild works
    (``obj-m += bitrev.o`` builds ``bitrev.ko``), so when the finding has the
    provider at m the name can be read off the path.  Composites are not
    guessed this way; those come from the build data or not at all.
    """
    known = rec.get("provider_module")
    if known:
        return known if known.endswith(".ko") else known + ".ko"
    levels = rec.get("levels") or {}
    if levels.get(rec.get("provider_option")) != "m":
        return None
    stem = os.path.basename(rec["provider"])
    if not stem.endswith(".o"):
        return None
    return stem[:-2] + ".ko"


def _visible_symbols(ctx: Context) -> set[str] | None:
    """The symbols that have a Kconfig prompt, from S1's variable map."""
    import os

    from kreach.util import read_json

    path = ctx.vars_file
    if not os.path.exists(path):
        return None
    try:
        return set(read_json(path).get("visible") or ())
    except Exception:
        return None


def build_findings(ctx: Context, results: list[dict]) -> list[dict]:
    meta = ctx.read_meta()
    kernel = meta.get("kernel_describe") or meta.get("kernel_commit", "unknown")
    cache: dict = {}
    fixes_cache: dict = {}
    maint_cache: dict = {}
    visible = _visible_symbols(ctx)
    findings: list[dict] = []
    for rec in results:
        if rec.get("status") != "confirmed":
            continue
        fix = suggest_fix(rec, ctx.ksrc, cache=cache, visible=visible)
        findings.append({
            "id": rec["id"],
            "arch": rec.get("arch", ctx.arch),
            "case": rec["case"],
            "caller": {"object": rec["caller"], "option": rec["caller_option"],
                       "source": rec.get("caller_source")},
            "provider": {"object": rec["provider"], "option": rec["provider_option"],
                         "module": module_name(rec)},
            "symbols": rec.get("failing_symbols") or rec["symbols"],
            "all_symbols": rec["symbols"],
            "witness": rec.get("witness"),
            # The spec's record holds the failing linker or modpost lines
            # under build_log; the file they came from is beside it so the
            # whole build can be re-read without rerunning anything.
            "build_log": rec.get("build_log_lines", []),
            "build_log_path": rec.get("build_log"),
            "build_seconds": rec.get("build_seconds"),
            "levels": rec.get("levels"),
            "fix": fix,
            "fixes": fixes_tag(ctx.ksrc, rec.get("caller_source"),
                               rec.get("failing_symbols") or rec["symbols"],
                               cache=fixes_cache),
            "maintainers": maintainers(ctx.ksrc, rec.get("caller_source"),
                                       cache=maint_cache),
            "kernel": kernel,
            "kernel_commit": meta.get("kernel_commit"),
        })
    findings.sort(key=lambda f: (f["caller"]["option"] or "", f["provider"]["option"] or ""))
    return findings


def group_by_caller(findings: list[dict]) -> dict[str, list[dict]]:
    """One report per caller Kconfig symbol, so one report becomes one patch."""
    groups: dict[str, list[dict]] = {}
    for f in findings:
        groups.setdefault(f["caller"]["option"] or "?", []).append(f)
    return groups


def render_text(ctx: Context, findings: list[dict]) -> str:
    """Plain-text summary per finding, pasteable into a commit message."""
    meta = ctx.read_meta()
    out: list[str] = []
    out.append("kreach findings -- %s, arch %s" % (meta.get("kernel_describe", "?"), ctx.arch))
    out.append("=" * 72)
    out.append("")
    if not findings:
        out.append("No confirmed findings.")
        out.append("")
        return "\n".join(out)

    for option, group in sorted(group_by_caller(findings).items()):
        out.append("## %s  (%d finding%s)" % (option, len(group), "" if len(group) == 1 else "s"))
        out.append("")
        for f in group:
            prov = f["provider"]
            out.append("  id          %s" % f["id"])
            out.append("  arch        %s" % f["arch"])
            out.append("  case        %s   (%s)" % (f["case"], _case_note(f["case"])))
            out.append("  caller      %s  [%s]" % (f["caller"]["object"], f["caller"]["option"]))
            out.append("  provider    %s  [%s]%s"
                       % (prov["object"], prov["option"],
                          ("  module %s" % prov["module"]) if prov.get("module") else ""))
            out.append("  symbols     %s" % ", ".join(f["symbols"]))
            out.append("  witness     %s" % (f["witness"] or "-"))
            if f.get("levels"):
                out.append("  levels      %s" % ", ".join(
                    "%s=%s" % kv for kv in sorted(f["levels"].items())))
            out.append("  build_log   %s" % (f.get("build_log_path") or "-"))
            for line in f.get("build_log", [])[:6]:
                out.append("      %s" % line)
            if f.get("fixes"):
                out.append("  %s" % f["fixes"]["tag"])
                out.append("              (%s)" % f["fixes"]["how"])
            if f.get("maintainers"):
                out.append("  send to     %s" % f["maintainers"][0])
                for who in f["maintainers"][1:]:
                    out.append("              %s" % who)
            out.append("  fix         %s" % f["fix"]["kconfig"])
            out.append("              in %s%s" % (f["fix"]["where"],
                                                  "   [LAST RESORT]" if f["fix"]["last_resort"] else ""))
            out.append("              %s" % f["fix"]["rationale"])
            if f["fix"].get("caveat"):
                out.append("  caveat      %s" % f["fix"]["caveat"])
            out.append("")
        out.append(_commit_block(ctx, group))
        out.append("")
    return "\n".join(out)


def _case_note(case: str) -> str:
    return {
        "y/m": "caller built-in, provider a module: vmlinux link error",
        "y/n": "caller built-in, provider disabled: vmlinux link error",
        "m/n": "caller a module, provider disabled: modpost error",
    }.get(case, case)


def _commit_block(ctx: Context, group: list[dict]) -> str:
    """The reproduction section for a commit message.

    One report is one patch, so the block covers every provider the caller is
    missing a constraint on, not just the first.
    """
    f = group[0]
    lines = ["  --- commit message reproduction section ---",
             "  |",
             "  | With the attached configuration%s (%s, %s):"
             % ("s" if len(group) > 1 else "", f["arch"], f["kernel"]),
             "  |"]
    for finding in group:
        shown = finding.get("build_log", [])[:3]
        if not shown:
            shown = ["undefined reference to `%s'" % finding["symbols"][0]]
        for line in shown:
            lines.append("  |   %s" % line)
    lines.append("  |")
    caller = f["caller"]["option"]
    for finding in group:
        prov = finding["provider"]["option"]
        level = "built into vmlinux" if finding["case"].startswith("y") else "a module"
        state = "a module" if finding["case"].endswith("m") else "disabled"
        lines.append("  | %s is %s while %s is %s, so %s"
                     % (caller, level, prov, state,
                        _symbol_phrase(finding["symbols"])))
        lines.append("  | not available at link time.")
    lines.append("  |")
    for finding in group:
        lines.append("  | Fix: %s in %s.%s"
                     % (finding["fix"]["kconfig"], finding["fix"]["where"],
                        "  [LAST RESORT]" if finding["fix"]["last_resort"] else ""))
        if finding["fix"].get("caveat"):
            lines.append("  | Note: %s" % finding["fix"]["caveat"])
    lines.append("  | Found with a static reachability check and confirmed by a build.")
    if f.get("fixes"):
        lines.append("  |")
        lines.append("  | %s" % f["fixes"]["tag"])
    lines.append("  ---")
    return "\n".join(lines)


def _symbol_phrase(symbols: list[str]) -> str:
    if len(symbols) == 1:
        return "%s is" % symbols[0]
    if len(symbols) <= 3:
        return "%s are" % ", ".join(symbols)
    return "%s and %d more are" % (", ".join(symbols[:3]), len(symbols) - 3)


def run_s7(ctx: Context, results: list[dict]) -> list[dict]:
    t0 = time.time()
    findings = build_findings(ctx, results)
    write_jsonl(ctx.findings_file, findings)
    text = render_text(ctx, findings)
    with open(ctx.report_file, "w") as fh:
        fh.write(text)

    meta = ctx.read_meta()
    by_case: dict[str, int] = {}
    by_fix: dict[str, int] = {}
    for f in findings:
        by_case[f["case"]] = by_case.get(f["case"], 0) + 1
        by_fix[f["fix"]["pattern"]] = by_fix.get(f["fix"]["pattern"], 0) + 1
    summary = {
        "arch": ctx.arch,
        "kernel": meta.get("kernel_describe"),
        "kernel_commit": meta.get("kernel_commit"),
        "findings": len(findings),
        "by_case": by_case,
        "by_fix_pattern": by_fix,
        "caller_options": len(group_by_caller(findings)),
        "stages": {k: v for k, v in meta.items() if k in ("s1", "s2", "s3", "s4", "s5", "s6")},
    }
    write_json(ctx.summary_file, summary)
    ctx.write_meta({"s7": {"findings": len(findings),
                           "seconds": round(time.time() - t0, 1)}})
    log.info("S7 complete: %d findings in %d caller options -> %s, %s",
             len(findings), summary["caller_options"],
             ctx.findings_file, ctx.report_file)
    return findings


# ---------------------------------------------------------------------------
# Re-running on a newer commit: still present / fixed / new.
# ---------------------------------------------------------------------------

def compare(previous_file: str, current_file: str) -> dict:
    prev = {f["id"]: f for f in read_jsonl(previous_file)}
    cur = {f["id"]: f for f in read_jsonl(current_file)}
    still = sorted(set(prev) & set(cur))
    fixed = sorted(set(prev) - set(cur))
    new = sorted(set(cur) - set(prev))
    return {
        "still_present": [_brief(cur[i]) for i in still],
        "fixed": [_brief(prev[i]) for i in fixed],
        "new": [_brief(cur[i]) for i in new],
        "counts": {"still_present": len(still), "fixed": len(fixed), "new": len(new)},
    }


def _brief(f: dict) -> dict:
    return {"id": f["id"], "case": f["case"],
            "caller_option": f["caller"]["option"],
            "provider_option": f["provider"]["option"],
            "symbols": f["symbols"], "kernel": f.get("kernel")}
