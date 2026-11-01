#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Historical recall for one benchmark case.
#
# Check out the parent of a commit that fixed a reachability bug, run the
# pipeline there, and see whether kreach reports the caller/provider pair the
# fix addressed.  One work directory per commit, because S1-S3 are per kernel
# commit.
#
#   tools/bench_replicate.sh [options] <ksrc> <workroot> <arch> <commit>
#
#     -d DIR   subtree for S3's wide build (repeatable, default: sound)
#     -s DIR   subtree for S2's presence table (repeatable; default is
#              kreach's own whole-tree default, which is right when the
#              provider lives outside the built subtree and costs ~14 min.
#              Pass -s for a case whose provider is in the same subtree.)
#     -c OPT   restrict S6 to this caller option (repeatable), so the build
#              budget goes on the pair the benchmark is about
#
# Needs the kernel tree on a branch it is safe to check out, and enough disk
# for one wide build plus one witness build per satisfiable query.
set -euo pipefail

BUILD_DIRS=()
S2_DIRS=()
CALLERS=()
while getopts "d:s:c:" opt; do
  case $opt in
    d) BUILD_DIRS+=("$OPTARG") ;;
    s) S2_DIRS+=("$OPTARG") ;;
    c) CALLERS+=("$OPTARG") ;;
    *) echo "usage: $0 [-d dir] [-s dir] [-c option] ksrc workroot arch commit" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

KSRC=${1:?kernel source tree}
WORKROOT=${2:?work directory root}
ARCH=${3:?arch}
COMMIT=${4:?commit that contains the fix}
[ ${#BUILD_DIRS[@]} -eq 0 ] && BUILD_DIRS=(sound)

PARENT=$(git -C "$KSRC" rev-parse "${COMMIT}^")
SHORT=$(git -C "$KSRC" rev-parse --short "$PARENT")
WORK="$WORKROOT/bench-$SHORT-$ARCH"
echo "== $COMMIT"
echo "   $(git -C "$KSRC" log -1 --format=%s "$COMMIT")"
echo "   parent $PARENT"
echo "   work   $WORK"

build_args=(); for d in "${BUILD_DIRS[@]}"; do build_args+=(-d "$d"); done
s2_args=();    for d in "${S2_DIRS[@]}";    do s2_args+=(-d "$d");    done
s6_args=();    for c in "${CALLERS[@]}";    do s6_args+=(--caller "$c"); done

git -C "$KSRC" checkout -q --detach "$PARENT"
echo "   kernel $(make -C "$KSRC" -s kernelversion)"

run() { echo "-- kreach $*"; kreach -k "$KSRC" -w "$WORK" -a "$ARCH" "$@"; }

run s1
run s2 "${s2_args[@]}"
run s3 --config allmodconfig "${build_args[@]}"
run s4
run s5
run s6 --ccache "${s6_args[@]}"
run s7 --quiet

# The benchmark is mined at the tip, where the whole history is present.
run score --benchmark "$WORKROOT/benchmark.json" --commit "$COMMIT" \
    -o "$WORK/score-$COMMIT.json"
