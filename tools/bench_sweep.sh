#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Run the historical-recall benchmark over a worklist, one case at a time.
#
#   tools/bench_sweep.sh <ksrc> <workroot> <arch> <worklist> <publish-dir>
#
# Each line of the worklist is "<commit> <caller option> <s3 subtree>".
# S1-S3 are per kernel commit and every case has a different parent, so there
# is nothing to share between them: the cost is one full model and one wide
# build each.  The work directory is deleted once the case is scored, so disk
# stays flat; the score and any confirmed finding are copied out first.
#
# Resumable: a case whose score file already exists is skipped, so rebuilding
# the worklist and rerunning picks up where it stopped.
set -uo pipefail

KSRC=${1:?kernel source}
WORKROOT=${2:?work root}
ARCH=${3:?arch}
LIST=${4:?worklist}
PUB=${5:?publish dir}
MIN_FREE_MB=${MIN_FREE_MB:-2048}

trap 'cd "$KSRC" && git checkout -q --detach v7.3 2>/dev/null; echo "RESTORED: $(git -C "$KSRC" describe --tags)"' EXIT

total=$(grep -c . "$LIST")
n=0
while read -r commit caller scope; do
  [ -z "${commit:-}" ] && continue
  n=$((n + 1))
  [ -f "$PUB/score-$commit.json" ] && { echo "[$n/$total] $commit already scored"; continue; }

  free=$(df --output=avail -m / | tail -1)
  if [ "$free" -lt "$MIN_FREE_MB" ]; then
    echo "STOPPING: ${free}MB free, below ${MIN_FREE_MB}MB"
    break
  fi

  parent=$(git -C "$KSRC" rev-parse "${commit}^" 2>/dev/null) || { echo "[$n/$total] $commit NO PARENT"; continue; }
  short=$(git -C "$KSRC" rev-parse --short "$parent")
  work="$WORKROOT/sweep-$short-$ARCH"
  echo "[$n/$total] === $commit  caller $caller  scope $scope  parent $short  (${free}MB free)"
  git -C "$KSRC" checkout -q --detach "$parent" || { echo "[$n/$total] CHECKOUT FAILED"; continue; }

  k() { ~/venv/bin/kreach -k "$KSRC" -w "$work" -a "$ARCH" "$@"; }
  fail=""
  k s1 >/dev/null 2>&1                                  || fail=s1
  [ -z "$fail" ] && { k s2 >/dev/null 2>&1              || fail=s2; }
  [ -z "$fail" ] && { k s3 --config allmodconfig -d "$scope" >/dev/null 2>&1 || fail=s3; }
  [ -z "$fail" ] && { k s4 >/dev/null 2>&1              || fail=s4; }
  [ -z "$fail" ] && { k s5 --caller "$caller" >/dev/null 2>&1 || fail=s5; }
  if [ -n "$fail" ]; then
    echo "[$n/$total] $commit FAILED at $fail"
    rm -rf "$work"
    continue
  fi

  k s6 --ccache --build-jobs 3 --caller "$caller" >/dev/null 2>&1
  k s7 --quiet --publish "$PUB/case-$commit" >/dev/null 2>&1
  if k score --benchmark "$WORKROOT/benchmark.json" --commit "$commit" \
             -o "$work/score-$commit.json" >/dev/null 2>&1; then
    cp "$work/score-$commit.json" "$PUB/"
    verdict=$(~/venv/bin/python -c "
import json; d=json.load(open('$work/score-$commit.json'))
print('HIT' if d['hits'] else ('excluded' if d.get('excluded') else 'miss'))" 2>/dev/null)
    echo "[$n/$total] $commit $verdict"
  else
    echo "[$n/$total] $commit SCORE FAILED"
  fi
  rm -rf "$work"
done < "$LIST"
echo "SWEEP DONE"
