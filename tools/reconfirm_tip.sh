#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Re-confirm a known set of findings against whatever the tree is checked out
# at, in a work directory of its own.
#
#   tools/reconfirm_tip.sh <ksrc> <work> <arch> <caller option>...
#
# S2 is scoped to the subtrees the providers actually live in, which is much
# faster than the whole tree and enough when the pairs are already known.
set -euo pipefail

KSRC=${1:?kernel source}
WORK=${2:?work dir}
ARCH=${3:?arch}
shift 3
CALLERS=("$@")

k() { echo "== kreach $*"; kreach -k "$KSRC" -w "$WORK" -a "$ARCH" "$@"; }

k s1
# sound/ for the callers, lib/ and drivers/base for BITREVERSE and REGMAP_MMIO.
k s2 -d sound -d lib -d drivers/base
k s3 --config allmodconfig -d sound
k s4

# Solve and build only the pairs asked for.  Without the filter S5 would
# work through every candidate in the tree, which for a re-confirmation of a
# known set is an hour spent to answer a question nobody asked.
cargs=(); for c in "${CALLERS[@]}"; do cargs+=(--caller "$c"); done
k s5 "${cargs[@]}"
k s6 --ccache --build-jobs 3 "${cargs[@]}"
k s7 --quiet
