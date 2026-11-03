# Ready to send

One patch, prepared but **not sent**. It needs your `Signed-off-by:`, which
is a certification under the Developer's Certificate of Origin about where
the change came from — not something anyone can add on your behalf.

## Before sending

```bash
cd ~/src/linux
git checkout --detach v7.3
git am outgoing/0001-drm-xe-fix-CONFIGFS_FS-dependency.patch
git commit --amend -s          # adds your Signed-off-by
git format-patch -1 -o outgoing
```

Then check it once more and send:

```bash
./scripts/checkpatch.pl outgoing/0001-*.patch
git send-email --to=matthew.brost@intel.com \
  --to=thomas.hellstrom@linux.intel.com \
  --to=rodrigo.vivi@intel.com \
  --cc=airlied@gmail.com --cc=simona@ffwll.ch \
  --cc=intel-xe@lists.freedesktop.org \
  --cc=dri-devel@lists.freedesktop.org \
  --cc=linux-kernel@vger.kernel.org \
  outgoing/0001-*.patch
```

`git send-email` has no transport configured in that tree, so it will need
`sendemail.smtpserver` and friends set up first.

Recipients are `scripts/get_maintainer.pl drivers/gpu/drm/xe/Kconfig`:
maintainers in `--to`, the lists and wider reviewers in `--cc`, which is the
usual split.

## State

- `checkpatch.pl --no-signoff` is clean
- applies to v7.3 at `drivers/gpu/drm/xe/Kconfig`, one line added
- the bug is confirmed by a real link failure at v7.3, and the fix is
  confirmed to make the configuration unreachable — see
  [../results/incidental/](../results/incidental/)
- authored as `InstructorZer0 <jude.gyimah87@gmail.com>`

## What is not done

The ten `sound/` findings in [../results/v7.3/](../results/v7.3/) are not
prepared as patches. They need `select`, not the `depends on` their records
carry, and `SND_SOC_FSL_AUD2HTX` needs two selects — worth doing as a series
per subsystem rather than ten singletons.
