#!/bin/bash
# Confirm the drm/xe finding at v7.3 from a minimal configuration, then show
# that the one-line fix makes the bad combination unreachable.
set -uo pipefail
K=~/src/linux; O=~/xe73; L=~/xe-confirm.log
: > "$L"; say() { echo "$*" | tee -a "$L"; }
cd "$K" || exit 1
git checkout -- . 2>/dev/null; git checkout -q --detach v7.3
say "tree: $(git describe --tags)   free: $(df --output=avail -m / | tail -1)MB"

mkfrag() { cat > "$1" <<EOF
CONFIG_DRM_XE=y
CONFIG_CONFIGFS_FS=m
CONFIG_DRM=y
CONFIG_PCI=y
CONFIG_MODULES=y
EOF
}

rm -rf "$O"; mkdir -p "$O"; mkfrag "$O/frag"
make -s O="$O" KCONFIG_ALLCONFIG="$O/frag" allnoconfig >/dev/null 2>&1
xe=$(grep -c "^CONFIG_DRM_XE=y" "$O/.config"); cf=$(grep -c "^CONFIG_CONFIGFS_FS=m" "$O/.config")
say "levels: DRM_XE=y($xe) CONFIGFS_FS=m($cf)"
if [ "$xe" -ne 1 ] || [ "$cf" -ne 1 ]; then
  say "ABORT: the y/m combination did not materialise, nothing to confirm"
  say "XE CONFIRM DONE"; exit 1
fi

say "linking vmlinux..."; t0=$(date +%s)
make O="$O" -j3 vmlinux > "$O/build.log" 2>&1; rc=$?
say "rc=$rc in $(( $(date +%s) - t0 ))s   free: $(df --output=avail -m / | tail -1)MB"
if grep -qa "No space left on device" "$O/build.log"; then
  say "INCONCLUSIVE: ran out of disk"
else
  u=$(grep -aoE "undefined reference to .[a-z_]*xe_configfs[a-z_]*." "$O/build.log" | sort -u | tr '\n' ' ')
  say "xe_configfs undefined references: ${u:-none}"
  [ "$rc" -ne 0 ] && grep -a "undefined reference" "$O/build.log" | head -3 | sed 's/^/  /' | tee -a "$L"
  if [ "$rc" -ne 0 ] && [ -n "$u" ]; then say "CONFIRMED at v7.3"
  elif [ "$rc" -eq 0 ]; then say "NOT CONFIRMED: it links cleanly at v7.3"
  else say "build failed for another reason"; fi
fi

say "--- with 'depends on CONFIGFS_FS || !CONFIGFS_FS' added to DRM_XE"
python3 - <<'PY'
p = "/home/ubuntu/src/linux/drivers/gpu/drm/xe/Kconfig"
s = open(p).read()
s = s.replace("\tdepends on KUNIT || !KUNIT\n",
              "\tdepends on KUNIT || !KUNIT\n\tdepends on CONFIGFS_FS || !CONFIGFS_FS\n", 1)
open(p, "w").write(s)
PY
rm -rf "$O"; mkdir -p "$O"; mkfrag "$O/frag"
make -s O="$O" KCONFIG_ALLCONFIG="$O/frag" allnoconfig >/dev/null 2>&1
say "levels with the fix: $(grep -E '^CONFIG_(DRM_XE|CONFIGFS_FS)=' "$O/.config" | tr '\n' ' ')"
grep -q "^CONFIG_DRM_XE=y" "$O/.config" && grep -q "^CONFIG_CONFIGFS_FS=m" "$O/.config" \
  && say "the fix does NOT prevent the combination" \
  || say "the fix makes the bad combination unreachable"
cd "$K" && git checkout -- drivers/gpu/drm/xe/Kconfig
rm -rf "$O"
say "XE CONFIRM DONE"
