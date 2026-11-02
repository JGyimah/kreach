# SPDX-License-Identifier: GPL-2.0-or-later
"""Build a sweep worklist from a mined benchmark.

  tools/bench_worklist.py <benchmark.json> <ksrc> <publish dir> > worklist

One line per case still to run:

  <commit> <caller option> <s3 scope,...> <provider option,...>

The scope is the interesting part.  S3 has to compile the object that makes
the reference, and the directory of the Kconfig file a fix edits is not
reliably where that object lives: mlx5's MLX5_MACSEC sits in
drivers/net/ethernet/mellanox/mlx5/core/Kconfig and gates
drivers/infiniband/hw/mlx5/macsec.o, in another subsystem entirely.  Scoping
by the Kconfig directory built the wrong tree and the case was recorded as a
miss with the solver never asked.

So the scope follows the symbols instead: whatever files reference them at
the parent commit, which is what the commit message named and what S3 needs
compiled.  The Kconfig directory comes along, and is the fallback when a case
names no symbol or names one referenced too widely to scope by.
"""

import argparse
import json
import os
import subprocess
import sys

# Where kernel code lives; include/ holds no objects, Documentation/ no code.
CODE_ROOTS = ("drivers/", "sound/", "net/", "fs/", "kernel/", "lib/", "crypto/",
              "mm/", "block/", "security/", "arch/", "virt/", "ipc/", "init/")
MAX_SCOPES = 6


def referencing_dirs(ksrc: str, rev: str, symbol: str, cache: dict) -> set[str]:
    key = (rev, symbol)
    if key in cache:
        return cache[key]
    out = subprocess.run(["git", "grep", "-l", symbol, rev],
                         cwd=ksrc, capture_output=True, text=True).stdout
    dirs = set()
    for line in out.splitlines():
        if ":" not in line:
            continue
        path = line.split(":", 1)[1]
        if not path.startswith(CODE_ROOTS):
            continue
        d = os.path.dirname(path)
        if d:
            dirs.add(d)
    cache[key] = dirs
    return dirs


def collapse(dirs: set[str]) -> list[str]:
    """Drop any directory an ancestor already covers."""
    out = []
    for d in sorted(dirs, key=lambda s: (s.count("/"), s)):
        if not any(d == k or d.startswith(k + "/") for k in out):
            out.append(d)
    return out


def scope_for(ksrc: str, case: dict, cache: dict) -> tuple[list[str], str]:
    kconfig_dirs = {os.path.dirname(f) for f in case.get("files", []) if "/" in f}
    kconfig_dirs = {d for d in kconfig_dirs if d}
    parent = case.get("parent")
    symbols = case.get("symbols") or []
    if not parent or not symbols:
        return collapse(kconfig_dirs), "no symbols; scoped by the Kconfig directory"

    found: set[str] = set()
    for sym in symbols:
        found |= referencing_dirs(ksrc, parent, sym, cache)
    if not found:
        return collapse(kconfig_dirs), "symbols reference nothing in-tree"

    scopes = collapse(found | kconfig_dirs)
    if len(scopes) > MAX_SCOPES:
        # A widely referenced symbol would have S3 build half the tree.
        return collapse(kconfig_dirs), ("%d directories reference these symbols; "
                                        "scoped by the Kconfig directory" % len(scopes))
    return scopes, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("benchmark")
    ap.add_argument("ksrc")
    ap.add_argument("publish")
    args = ap.parse_args()

    bench = json.load(open(args.benchmark))
    done = set()
    if os.path.isdir(args.publish):
        done = {f[len("score-"):-len(".json")] for f in os.listdir(args.publish)
                if f.startswith("score-") and f.endswith(".json")}

    cache: dict = {}
    rows, notes = [], []
    for case in bench["cases"]:
        if not (case.get("caller_option") and case.get("provider_options")):
            continue
        sha = case["commit"][:12]
        if case.get("excluded") or sha in done:
            continue
        scopes, note = scope_for(args.ksrc, case, cache)
        if not scopes:
            notes.append("%s skipped: no scope could be derived" % sha)
            continue
        rows.append("%s %s %s %s" % (sha, case["caller_option"], ",".join(scopes),
                                     ",".join(case["provider_options"])))
        if note:
            notes.append("%s %s" % (sha, note))
    print("\n".join(rows))
    sys.stderr.write("to run: %d\n" % len(rows))
    for n in notes:
        sys.stderr.write("  %s\n" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
