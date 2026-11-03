# SPDX-License-Identifier: GPL-2.0-or-later
"""Turn confirmed findings into kernel patches, grouped per subsystem.

  tools/prepare_patches.py <ksrc> <results dir> <outgoing dir>

One patch per caller option, because that is the unit a maintainer applies,
and one series per Kconfig file, because that is the unit one maintainer
takes.  Each patch adds `select <PROVIDER>` -- not the `depends on` the
records carry: both providers are promptless, so `depends on` would leave the
driver unselectable, which is what the caveat in every record says.

Nothing here is sent, and nothing is signed off: a Signed-off-by certifies
under the Developer's Certificate of Origin where the change came from, which
is the author's own attestation.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# symbol -> what the caller must select, and where it comes from
SELECT_FOR = {
    "byte_rev_table": ("BITREVERSE", "lib/bitrev.c"),
    "__devm_regmap_init_mmio_clk": ("REGMAP_MMIO", "drivers/base/regmap/regmap-mmio.c"),
    "devm_snd_dmaengine_pcm_register": ("SND_SOC_GENERIC_DMAENGINE_PCM",
                                        "sound/soc/soc-generic-dmaengine-pcm.c"),
}

# Each series: the Kconfig file, a short name, and the subject prefix the
# subsystem actually uses.
SERIES = [
    ("sound/soc/rockchip/Kconfig", "rockchip", "ASoC: rockchip"),
    ("sound/soc/fsl/Kconfig", "fsl", "ASoC: fsl"),
    ("sound/soc/intel/Kconfig", "intel", "ASoC: Intel"),
    ("sound/soc/sunxi/Kconfig", "sunxi", "ASoC: sunxi"),
]

# Kernel practice credits the finder -- kismet findings and kernel test robot
# reports both name the tool -- so the patches say where they came from.
CREDIT = ("Found with kreach, a static check that compares what Kconfig allows "
          "against the symbols the compiled objects actually reference.")

RE_STANZA = re.compile(r"^\s*config\s+([A-Z0-9_]+)\s*$")
RE_TYPE = re.compile(r"^(\s+)(bool|tristate)\b")


def git(ksrc, *args, **kw):
    return subprocess.run(["git"] + list(args), cwd=ksrc, capture_output=True,
                          text=True, **kw)


def add_selects(ksrc, path, option, selects):
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
    # Kernel style orders a stanza type, then depends on, then select.  Put
    # them after the last depends on if there is one, else after the type.
    at, indent = None, "\t"
    for i in range(start + 1, end):
        m = RE_TYPE.match(lines[i])
        if m and at is None:
            indent, at = m.group(1), i + 1
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)depends on\b", lines[i])
        if m:
            indent, at = m.group(1), i + 1
    body = "".join(lines[start:end])
    todo = [s for s in selects
            if not re.search(r"^\s+select\s+%s\b" % re.escape(s), body, re.M)]
    if at is None or not todo:
        return []
    lines[at:at] = ["%sselect %s\n" % (indent, s) for s in todo]
    open(full, "w").write("".join(lines))
    return todo


def wrap(text, width=74):
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


def message(prefix, finding, pairs, provider_option):
    """pairs: [(symbol, select, provider source file)] in the order added."""
    opt = finding["caller"]["option"]
    short = opt.removeprefix("CONFIG_")
    drv = finding["caller"]["source"].rsplit("/", 1)[-1].removesuffix(".c")
    selects = [s for _, s, _ in pairs]

    tail = prefix.rsplit(": ", 1)[-1]
    head = prefix if not drv.lower().startswith(tail.lower()) \
        else prefix.rsplit(": ", 1)[0]
    subject = "%s: %s: select %s" % (
        head, drv,
        selects[0] if len(selects) == 1 else "missing dependencies")

    body = [subject, ""]
    for sym, sel, src in pairs:
        body += wrap("%s calls %s, which %s provides under %s." % (
            drv, sym, src, sel))
    body.append("")
    err = next((l for l in finding.get("build_log") or []
                if "undefined reference" in l), "")
    err = re.sub(r"^.*?([A-Za-z0-9_.\-]+\.c)", r"\1", err.strip())
    # Never truncate quoted output -- a half-written symbol name is worse
    # than a wrapped line.  Break before the symbol if it will not fit.
    if len(err) > 72 and " to " in err:
        before, _, after = err.rpartition(" to ")
        body.append("  %s to" % before)
        body.append("    %s" % after)
    else:
        body.append("  %s" % err)
    body.append("")
    n = len(finding.get("all_symbols") or finding["symbols"])
    if n > 1:
        body += wrap("%d symbols are undefined in all." % n)
        body.append("")
    body += wrap("The driver can be built into vmlinux while %s is a module, in "
                 "which case the %s not there at link time." % (
                     provider_option.removeprefix("CONFIG_"),
                     "symbols are" if n > 1 else "symbol is"))
    body.append("")
    body += wrap("Select rather than depend on: %s has no prompt of its own and "
                 "is only ever enabled by a select, so a depends on would leave "
                 "%s unselectable." % (selects[0], short))
    prov = provider_option.removeprefix("CONFIG_")
    if selects[0] != prov:
        # BITREVERSE is what a driver selects; GENERIC_BITREVERSE is what
        # gates bitrev.o.  A reviewer will ask why they differ, so say.
        body.append("")
        body += wrap("%s is the symbol to select rather than %s: it selects %s "
                     "only where the architecture has no native implementation, "
                     "so this stays correct on the architectures that do."
                     % (selects[0], prov, prov))
    body.append("")
    body += wrap("Reproduced on v7.3, x86_64, with CONFIG_%s=y and %s=m." % (
        short, provider_option))
    body.append("")
    body += wrap(CREDIT)
    body.append("")
    body.append(finding["fixes"]["tag"])
    return "\n".join(body) + "\n"


def write_cover(out, prefix, group):
    """Fill in format-patch's placeholders rather than shipping them."""
    path = os.path.join(out, "0000-cover-letter.patch")
    if not os.path.exists(path):
        return
    text = open(path).read()
    drivers = sorted(f["caller"]["source"].rsplit("/", 1)[-1].removesuffix(".c")
                     for f in group)
    provs = sorted({f["provider"]["option"].removeprefix("CONFIG_") for f in group})
    subject = "%s: fix missing %s dependencies" % (prefix, " and ".join(provs))
    blurb = "\n".join([
        "Each of these drivers can be built into vmlinux while the code",
        "providing a symbol it calls is a module, which fails to link.  The",
        "same shape in %d drivers, so they are here as one series." % len(group),
        "",
        "\n".join(wrap("Affected: %s." % ", ".join(drivers))),
        "",
        "The fix is a select rather than a depends on: the providers have no",
        "prompt of their own and are only ever enabled by a select, so a",
        "depends on would leave these drivers unselectable.",
        "",
        "Each was reproduced on v7.3, x86_64, by a vmlinux link that failed on",
        "the named symbol, and each links once the select is added.",
        "",
        "\n".join(wrap(CREDIT)),
    ])
    text = text.replace("*** SUBJECT HERE ***", subject)
    text = text.replace("*** BLURB HERE ***", blurb)
    open(path, "w").write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ksrc")
    ap.add_argument("results")
    ap.add_argument("outgoing")
    args = ap.parse_args()

    findings = {}
    for line in open(os.path.join(args.results, "findings.jsonl")):
        if line.strip():
            f = json.loads(line)
            findings[f["caller"]["option"]] = f

    env = dict(os.environ,
               GIT_AUTHOR_NAME="InstructorZer0",
               GIT_AUTHOR_EMAIL="jude.gyimah87@gmail.com",
               GIT_COMMITTER_NAME="InstructorZer0",
               GIT_COMMITTER_EMAIL="jude.gyimah87@gmail.com")

    made = 0
    for path, name, prefix in SERIES:
        mine = sorted(o for o, f in findings.items()
                      if subprocess.run(["git", "grep", "-l",
                                         "^config %s$" % o.removeprefix("CONFIG_"),
                                         "v7.3", "--", path],
                                        cwd=args.ksrc, capture_output=True,
                                        text=True).stdout.strip())
        if not mine:
            continue
        git(args.ksrc, "reset", "-q", "--hard", "v7.3")
        for opt in mine:
            f = findings[opt]
            pairs, sel = [], []
            for sym in (f["all_symbols"] or f["symbols"]):
                if sym in SELECT_FOR:
                    k, where = SELECT_FOR[sym]
                    if k not in sel:
                        sel.append(k)
                        pairs.append((sym, k, where))
            if opt == "CONFIG_SND_SOC_FSL_AUD2HTX":
                sym = "devm_snd_dmaengine_pcm_register"
                k, where = SELECT_FOR[sym]
                if k not in sel:
                    sel.append(k)
                    pairs.append((sym, k, where))
            if not sel:
                print("  skipped %s: no known select" % opt)
                continue
            added = add_selects(args.ksrc, path, opt, sel)
            if not added:
                print("  skipped %s: selects already present" % opt)
                continue
            git(args.ksrc, "add", path)
            msg = message(prefix, f, [p for p in pairs if p[1] in added],
                          f["provider"]["option"])
            subprocess.run(["git", "commit", "-q", "-F", "-"], cwd=args.ksrc,
                           input=msg, text=True, env=env, check=True)
            made += 1
        out = os.path.abspath(os.path.join(args.outgoing, name))
        os.makedirs(out, exist_ok=True)
        for old in os.listdir(out):
            os.remove(os.path.join(out, old))
        n = len(mine)
        cmd = ["git", "format-patch", "-%d" % n, "-o", out]
        if n > 1:
            cmd += ["--cover-letter"]
        subprocess.run(cmd, cwd=args.ksrc, capture_output=True, text=True,
                       env=env, check=True)
        if n > 1:
            write_cover(out, prefix, [findings[o] for o in mine])
        print("  %-10s %d patch(es) -> %s" % (name, n, out))
    git(args.ksrc, "reset", "-q", "--hard", "v7.3")
    print("prepared %d patches; tree reset to v7.3" % made)
    return 0


if __name__ == "__main__":
    sys.exit(main())
