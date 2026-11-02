#!/bin/bash
# Wait for the benchmark sweep, then check the fixes: per-witness links, then
# the stock configurations and an allmodconfig vmlinux with every fix applied.
set -uo pipefail
echo "waiting for the sweep to finish..."
while true; do
  if grep -aqE "SWEEP DONE|STOPPING:" ~/sweep.log 2>/dev/null; then break; fi
  sleep 300
done
echo "sweep finished at $(date -Is)"

cd ~/src/linux
git checkout -- . 2>/dev/null
git checkout -q --detach v7.3
echo "tree: $(git describe --tags)  clean: $(git status --porcelain | wc -l) changes"
df -h / | tail -1

rm -rf ~/fixbuild
~/venv/bin/python ~/kreach/tools/verify_fix.py ~/src/linux ~/kreach/results/v7.3 \
    ~/fixbuild -o ~/kreach/results/v7.3/fix-verification.json --jobs 3 --regression
echo "VERIFY DONE rc=$?"

cd ~/src/linux && git checkout -- . 2>/dev/null; git checkout -q --detach v7.3
echo "tree restored: $(git describe --tags)  clean: $(git status --porcelain | wc -l) changes"
rm -rf ~/fixbuild
df -h / | tail -1
