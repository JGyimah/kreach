# SPDX-License-Identifier: GPL-2.0-or-later
"""Does the suggested fix actually make the witness link?

kreach confirms a finding by a build that fails.  That is not the same as
showing the patch works, which is the first thing a maintainer asks.  For
each finding this applies the fix to the caller's Kconfig stanza, hands the
witness back to Kconfig, and links vmlinux again.

  tools/verify_fix.py <ksrc> <results dir> <build dir> [-o report.json]

The fix is `select <PROVIDER>`, not the `depends on` the records carry: both
providers are promptless, so `depends on` would leave the driver
unselectable.  For byte_rev_table the select is BITREVERSE rather than
GENERIC_BITREVERSE -- BITREVERSE is what a driver selects, and it decides
whether the generic implementation is needed at all.

``--regression`` then applies every fix at once and asks the other question a
maintainer has: does the series break a configuration it was not aimed at?
A `select` is the construct that creates unmet-dependency bugs, so the cheap
and decisive part is Kconfig itself -- allnoconfig, defconfig, allmodconfig
and allyesconfig are each seconds and surface exactly those warnings.  It
then links vmlinux under allmodconfig, which builds the built-in part of the
tree; the modules are the bulk and are not what a y/m fix can break.  A
whole-tree `make all` under allmodconfig does not fit on a 24 GB disk and is
reported as skipped rather than attempted.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

# symbol -> what the caller must select to provide it
SELECT_FOR = {
    "byte_rev_table": "BITREVERSE",
    "__devm_regmap_init_mmio_clk": "REGMAP_MMIO",
    "devm_snd_dmaengine_pcm_register": "SND_SOC_GENERIC_DMAENGINE_PCM",
}

RE_STANZA = re.compile(r"^\s*(?:menu)?config\s+([A-Z0-9_]+)\s*$")
RE_TYPE = re.compile(r"^(\s+)(bool|tristate)\b")


def find_kconfig(ksrc: str, option: str) -> str | None:
    name = option.removeprefix("CONFIG_")
    out = subprocess.run(["grep", "-rl", "^config %s$" % name, "sound/", "drivers/"],
                         cwd=ksrc, capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def add_selects(ksrc: str, path: str, option: str, selects: list[str]) -> list[str]:
    """Insert `select X` into the stanza, after its type line.  Returns the
    selects actually added (one already present is left alone)."""
    name = option.removeprefix("CONFIG_")
    full = os.path.join(ksrc, path)
    lines = open(full).read().splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines)
                  if RE_STANZA.match(l) and RE_STANZA.match(l).group(1) == name), None)
    if start is None:
        raise SystemExit("no stanza for %s in %s" % (option, path))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace():
            end = i
            break
    body = "".join(lines[start:end])
    todo, indent, at = [], "\t", None
    for i in range(start + 1, end):
        m = RE_TYPE.match(lines[i])
        if m and at is None:
            indent, at = m.group(1), i + 1
    for s in selects:
        if re.search(r"^\s+select\s+%s\b" % re.escape(s), body, re.M):
            continue
        todo.append(s)
    if at is None or not todo:
        return []
    lines[at:at] = ["%sselect %s\n" % (indent, s) for s in todo]
    open(full, "w").write("".join(lines))
    return todo


def kconfig_warnings(log: str) -> list[str]:
    """Kconfig's own complaints, which is where a bad `select` shows up."""
    keep = ("warning:", "unmet direct dependencies", "recursive dependency",
            "symbol value", "is not a valid")
    return sorted({l.strip() for l in log.splitlines()
                   if any(k in l.lower() for k in keep)})


def regression(ksrc: str, findings: list[dict], build: str, jobs: int,
               min_free_mb: int = 5000) -> dict:
    """Apply every fix at once, then see what Kconfig and the linker say."""
    edited: list[str] = []
    out: dict = {"configs": {}, "selects_applied": {}}
    try:
        for f in findings:
            opt = f["caller"]["option"]
            syms = f["all_symbols"] or f["symbols"]
            selects = sorted({SELECT_FOR[s] for s in syms if s in SELECT_FOR})
            if opt == "CONFIG_SND_SOC_FSL_AUD2HTX":
                selects = sorted(set(selects) | {"SND_SOC_GENERIC_DMAENGINE_PCM"})
            path = find_kconfig(ksrc, opt)
            if not path or not selects:
                continue
            added = add_selects(ksrc, path, opt, selects)
            if added:
                edited.append(path)
                out["selects_applied"][opt] = added
        edited = sorted(set(edited))
        out["files_touched"] = edited
        print("\n== regression: %d stanzas patched in %d files"
              % (len(out["selects_applied"]), len(edited)), flush=True)

        for target in ("allnoconfig", "defconfig", "allmodconfig", "allyesconfig"):
            p = subprocess.run(["make", "O=" + build, target], cwd=ksrc,
                               capture_output=True, text=True, check=False)
            log = (p.stdout or "") + (p.stderr or "")
            warn = kconfig_warnings(log)
            out["configs"][target] = {"rc": p.returncode, "warnings": warn}
            print("   %-13s rc=%d %s" % (target, p.returncode,
                                         "WARNINGS: %s" % warn[:3] if warn else "clean"),
                  flush=True)

        free = shutil.disk_usage("/").free // (1024 * 1024)
        if free < min_free_mb:
            out["allmodconfig_vmlinux"] = {
                "result": "skipped", "reason": "%dMB free, below %dMB" % (free, min_free_mb)}
            print("   vmlinux       skipped: %dMB free" % free, flush=True)
            return out

        subprocess.run(["make", "-s", "O=" + build, "allmodconfig"], cwd=ksrc,
                       capture_output=True, text=True, check=False)
        t0 = time.time()
        p = subprocess.run(["make", "O=" + build, "-j%d" % jobs, "vmlinux"],
                           cwd=ksrc, capture_output=True, text=True, check=False)
        log = (p.stdout or "") + (p.stderr or "")
        undef = sorted(set(re.findall(
            r"undefined reference to [`\']([A-Za-z_][A-Za-z0-9_]*)\'", log)))
        out["allmodconfig_vmlinux"] = {
            "rc": p.returncode, "seconds": round(time.time() - t0, 1),
            "undefined": undef,
            "result": "links" if p.returncode == 0 else "fails",
            "tail": [] if p.returncode == 0 else
                    [l for l in log.splitlines() if l.strip()][-15:],
        }
        print("   vmlinux       %s in %.0fs%s" % (
            out["allmodconfig_vmlinux"]["result"],
            out["allmodconfig_vmlinux"]["seconds"],
            "  undefined: " + ", ".join(undef) if undef else ""), flush=True)
        return out
    finally:
        for path in edited:
            subprocess.run(["git", "checkout", "--", path], cwd=ksrc, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ksrc")
    ap.add_argument("results")
    ap.add_argument("build")
    ap.add_argument("-o", "--output")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--regression", action="store_true",
                    help="also apply every fix at once and check the stock "
                         "configurations plus an allmodconfig vmlinux link")
    args = ap.parse_args()

    findings = [json.loads(l) for l in
                open(os.path.join(args.results, "findings.jsonl")) if l.strip()]
    os.makedirs(args.build, exist_ok=True)
    report = []

    for f in findings:
        opt = f["caller"]["option"]
        wit = os.path.join(args.results, f["witness"])
        syms = f["all_symbols"] or f["symbols"]
        selects = sorted({SELECT_FOR[s] for s in syms if s in SELECT_FOR})
        # The second symbol in AUD2HTX's log is not in the record's own list.
        if opt == "CONFIG_SND_SOC_FSL_AUD2HTX":
            selects = sorted(set(selects) | {"SND_SOC_GENERIC_DMAENGINE_PCM"})
        path = find_kconfig(args.ksrc, opt)
        rec = {"caller_option": opt, "kconfig": path, "selects": selects,
               "symbols": syms}
        print("== %s  select %s" % (opt, ", ".join(selects)), flush=True)

        if not path or not selects:
            rec["result"] = "skipped: no stanza or no known select"
            report.append(rec)
            continue

        added = add_selects(args.ksrc, path, opt, selects)
        rec["added"] = added
        try:
            shutil.copy(wit, os.path.join(args.build, ".config"))
            t0 = time.time()
            subprocess.run(["make", "-s", "O=" + args.build, "olddefconfig"],
                           cwd=args.ksrc, capture_output=True, text=True, check=False)
            cfg = open(os.path.join(args.build, ".config")).read()
            rec["provider_level_after_fix"] = {
                s: (re.search(r"^CONFIG_%s=(\S+)$" % s, cfg, re.M) or [None, "n"])[1]
                for s in selects}
            p = subprocess.run(["make", "O=" + args.build, "-j%d" % args.jobs, "vmlinux"],
                               cwd=args.ksrc, capture_output=True, text=True, check=False)
            log = (p.stdout or "") + (p.stderr or "")
            undef = sorted(set(re.findall(
                r"undefined reference to [`']([A-Za-z_][A-Za-z0-9_]*)'", log)))
            rec.update({
                "seconds": round(time.time() - t0, 1),
                "rc": p.returncode,
                "undefined_after_fix": undef,
                "result": "links" if p.returncode == 0 else "still fails",
            })
            if p.returncode != 0:
                rec["tail"] = [l for l in log.splitlines() if l.strip()][-12:]
            print("   -> %s in %.0fs%s" % (
                rec["result"], rec["seconds"],
                "  undefined: " + ", ".join(undef) if undef else ""), flush=True)
        finally:
            subprocess.run(["git", "checkout", "--", path], cwd=args.ksrc, check=False)
        report.append(rec)

    links = sum(1 for r in report if r.get("result") == "links")
    summary = {"findings": len(report), "links": links, "cases": report}
    print("\n== %d of %d witnesses link once the fix is applied" % (links, len(report)))
    if args.regression:
        summary["regression"] = regression(args.ksrc, findings, args.build, args.jobs)
    if args.output:
        with open(args.output, "w") as fh:
            json.dump(summary, fh, indent=1, sort_keys=True)
        print("   wrote %s" % args.output)
    return 0 if links == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
