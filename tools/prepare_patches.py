# SPDX-License-Identifier: GPL-2.0-or-later
"""Turn confirmed findings into kernel patches, grouped per subsystem.

  tools/prepare_patches.py <ksrc> <results dir> <outgoing dir>

One patch per caller option, because that is the unit a maintainer applies,
and one series per Kconfig file, because that is the unit one maintainer
takes.  Each patch adds `select <PROVIDER>` -- not the `depends on` the
records carry: both providers are promptless, so `depends on` would leave the
driver unselectable, which is what the caveat in every record says.

Nothing here is sent.  Signing off is opt-in via --signoff, because a
Signed-off-by certifies under the Developer's Certificate of Origin where the
change came from: it is the author's own attestation, made deliberately, not
a formatting detail the tool should add on its behalf.
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
    "snd_pcm_new": ("SND_PCM", "sound/core/pcm.c"),
}

# Each series: the Kconfig file, a short name, and the subject prefix the
# subsystem actually uses.
SERIES = [
    ("sound/soc/rockchip/Kconfig", "rockchip", "ASoC: rockchip"),
    ("sound/soc/fsl/Kconfig", "fsl", "ASoC: fsl"),
    ("sound/soc/intel/Kconfig", "intel", "ASoC: Intel"),
    ("sound/soc/sunxi/Kconfig", "sunxi", "ASoC: sunxi"),
    ("sound/soc/meson/Kconfig", "meson", "ASoC: meson"),
    ("sound/pci/Kconfig", "pci", "ALSA"),
]

# Kernel practice credits the finder -- kismet findings and kernel test robot
# reports both name the tool -- so the patches say where they came from.
CREDIT = ("Found with kreach, a static check that compares what Kconfig allows "
          "against the symbols the compiled objects actually reference.")

# A patch-specific opening, where one patch has something to say that the
# others do not.  Not here to make the set read as independently written --
# five Rockchip patches with identical openings were applied without comment
# -- but because leading with the strongest fact about a patch is better than
# leading with the same sentence every time.  Verified at v7.3.
LEAD = {
    "CONFIG_SND_SOC_FSL_XCVR": [
        "SND_SOC_FSL_SPDIF, a few stanzas up in the same file, already selects",
        "BITREVERSE for the same reason.  SND_SOC_FSL_XCVR is missing it.",
    ],
    "CONFIG_SND_AW2": [
        "Thirty-six other stanzas in sound/pci/Kconfig select SND_PCM.",
        "SND_AW2 selects nothing at all.",
    ],
    "CONFIG_SND_MESON_AXG_PDM": [
        "Several stanzas in the same file already select REGMAP_MMIO,",
        "SND_MESON_G12A_TOACODEC among them.  These three do not.",
    ],
    "CONFIG_SND_MESON_AIU": [
        "gx-formatter.o is part of snd-soc-meson-aiu:",
        "",
        "  sound/soc/meson/Makefile:7: snd-soc-meson-aiu-y += gx-formatter.o",
        "",
        "so SND_MESON_AIU is the option that decides where its call lands,",
        "and the error below names gx-formatter.c rather than aiu.c.",
    ],
    "CONFIG_SND_SOC_FSL_AUD2HTX": [
        "This driver is short two selects, not one: its configuration fails to",
        "link on a symbol from each.",
    ],
}

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
    if opt in LEAD:
        body += LEAD[opt]
        body.append("")
    for sym, sel, src in pairs:
        body += wrap("%s calls %s, which %s provides under %s." % (
            drv, sym, src, sel))
    body.append("")
    # Prefer an error line naming this driver's own source.  A witness often
    # exposes several drivers at once, and quoting another file's error in
    # this patch invites the reasonable question of what the patch fixes.
    lines = [l for l in finding.get("build_log") or [] if "undefined reference" in l]
    base = drv + ".c"
    err = next((l for l in lines if base in l), lines[0] if lines else "")
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
    # The kernel and architecture come from the record, not from a constant.
    # Hardcoding x86_64 made the ARCH_MESON patches claim a reproduction on an
    # architecture that cannot build those drivers at all.
    body += wrap("Reproduced on %s, %s, with CONFIG_%s=y and %s=m." % (
        finding.get("kernel") or "v7.3", finding["arch"], short,
        provider_option))
    body.append("")
    body += wrap(CREDIT)
    body.append("")
    body.append(finding["fixes"]["tag"])
    return "\n".join(body) + "\n"


NOTE_MARK = "The checker is at "
NOTE = ("The checker is at https://github.com/JGyimah/kreach, and the\n"
        "reproducer configuration for each finding is published with it\n"
        "under results/.\n")


def add_note(path):
    """Put a reviewer note below the --- line.

    Below --- is the right place for it: git am drops everything there, so
    the note reaches whoever reads the mail without becoming part of the
    commit message.  A URL to a personal repository is useful to a reviewer
    today and dead weight in git log in five years.
    """
    text = open(path).read()
    if NOTE_MARK in text:
        return False
    marker = "\n---\n"
    i = text.find(marker)
    if i < 0:
        return False
    at = i + len(marker)
    open(path, "w").write(text[:at] + NOTE + "\n" + text[at:])
    return True


def note_group(out):
    """One note per posting: the cover letter if there is one, else the
    single patch.  Six copies of the same URL in a five-patch series is
    noise."""
    cover = os.path.join(out, "0000-cover-letter.patch")
    if os.path.exists(cover):
        # The cover letter carries it in the blurb, which write_cover
        # fills: there is no --- line to put it below, and nothing in a
        # cover letter becomes a commit message anyway.
        return []
    done = []
    for f in sorted(os.listdir(out)):
        if re.match(r"^\d{4}-.*\.patch$", f) and add_note(os.path.join(out, f)):
            done.append(os.path.join(out, f))
    return done


def write_cover(out, prefix, group):
    """Fill in format-patch's placeholders rather than shipping them."""
    path = os.path.join(out, "0000-cover-letter.patch")
    if not os.path.exists(path):
        return
    text = open(path).read()
    drivers = sorted(f["caller"]["source"].rsplit("/", 1)[-1].removesuffix(".c")
                     for f in group)
    provs = sorted({f["provider"]["option"].removeprefix("CONFIG_") for f in group})
    # From the records, for the same reason as in message(): a hardcoded
    # x86_64 claimed reproductions on an architecture that cannot build the
    # ARCH_MESON drivers.
    kernels = sorted({f.get("kernel") or "v7.3" for f in group})
    arches = sorted({f["arch"] for f in group})
    where = "%s, %s" % (" and ".join(kernels), " and ".join(arches))
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
        "\n".join(wrap("Each was reproduced on %s by a vmlinux link that "
                        "failed on the named symbol, and each links once the "
                        "select is added." % where)),
        "",
        "\n".join(wrap(CREDIT)),
        "",
        NOTE.rstrip("\n"),
    ])
    text = text.replace("*** SUBJECT HERE ***", subject)
    if "*** BLURB HERE ***" in text:
        text = text.replace("*** BLURB HERE ***", blurb)
    elif NOTE_MARK not in text:
        # Already filled by an earlier run but missing the note: put it
        # before the shortlog rather than rewriting what is there.
        m = re.search(r"^\S.*\(\d+\):$", text, re.M)
        if m:
            text = text[:m.start()] + NOTE + "\n" + text[m.start():]
    open(path, "w").write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ksrc")
    ap.add_argument("results")
    ap.add_argument("outgoing")
    ap.add_argument("--note-only", action="store_true",
                    help="only add the reviewer note below the --- line, to "
                         "the cover letter of each series or to a lone patch")
    ap.add_argument("--signoff", action="store_true",
                    help="add Signed-off-by from the tree's git identity.  "
                         "This is the author certifying the Developer's "
                         "Certificate of Origin, so it is never the default.")
    ap.add_argument("--cover-only", action="store_true",
                    help="only refill the cover letters in place.  Signing a "
                         "series off means re-running format-patch, which "
                         "rewrites the cover letter back to its placeholders; "
                         "this puts the subject and blurb back without "
                         "touching the signed patches.")
    args = ap.parse_args()

    findings = {}
    for line in open(os.path.join(args.results, "findings.jsonl")):
        if line.strip():
            f = json.loads(line)
            findings[f["caller"]["option"]] = f

    # Author as whoever the tree is configured as, so From: matches the
    # Signed-off-by: added later.  checkpatch warns when they differ, and a
    # patch whose author and signer look like two people invites the
    # question of who is actually certifying it.
    who = subprocess.run(["git", "config", "user.name"], cwd=args.ksrc,
                         capture_output=True, text=True).stdout.strip()
    mail = subprocess.run(["git", "config", "user.email"], cwd=args.ksrc,
                          capture_output=True, text=True).stdout.strip()
    if not who or not mail:
        raise SystemExit("set user.name and user.email in %s first" % args.ksrc)
    env = dict(os.environ,
               GIT_AUTHOR_NAME=who, GIT_AUTHOR_EMAIL=mail,
               GIT_COMMITTER_NAME=who, GIT_COMMITTER_EMAIL=mail)

    if args.note_only:
        groups = [os.path.abspath(args.outgoing)] + \
            [os.path.abspath(os.path.join(args.outgoing, n))
             for _, n, _ in SERIES]
        for g in groups:
            if os.path.isdir(g):
                for f in note_group(g):
                    print("  noted %s" % os.path.relpath(f, args.outgoing))
        return 0

    if args.cover_only:
        for path, name, prefix in SERIES:
            out = os.path.abspath(os.path.join(args.outgoing, name))
            cover = os.path.join(out, "0000-cover-letter.patch")
            if not os.path.exists(cover):
                continue
            group = [f for o, f in findings.items()
                     if subprocess.run(["git", "grep", "-l",
                                        "^config %s$" % o.removeprefix("CONFIG_"),
                                        "v7.3", "--", path],
                                       cwd=args.ksrc, capture_output=True,
                                       text=True).stdout.strip()]
            write_cover(out, prefix, group)
            filled = "*** SUBJECT HERE ***" not in open(cover).read()
            print("  %-10s cover letter %s" % (name, "filled" if filled else "STILL A TEMPLATE"))
        return 0

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
        if args.signoff:
            cmd += ["--signoff"]
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
