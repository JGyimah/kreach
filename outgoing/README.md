# Ready to send

Eleven patches in five groups, prepared but **not sent**. Each needs your
`Signed-off-by:`, which certifies under the Developer's Certificate of Origin
where the change came from — not something anyone can add on your behalf.

| group | patches | file touched |
|---|---|---|
| [`.`](.) | 1 — drm/xe | `drivers/gpu/drm/xe/Kconfig` |
| [`rockchip/`](rockchip/) | 5 + cover letter | `sound/soc/rockchip/Kconfig` |
| [`fsl/`](fsl/) | 3 + cover letter | `sound/soc/fsl/Kconfig` |
| [`intel/`](intel/) | 1 | `sound/soc/intel/Kconfig` |
| [`sunxi/`](sunxi/) | 1 | `sound/soc/sunxi/Kconfig` |

## State

- `checkpatch.pl --no-signoff` clean on all eleven
- each group applies to v7.3 and stacks in order
- no message line over 75 columns except `Fixes:` tags, which convention
  exempts
- every bug confirmed by a `vmlinux` link that failed on the named symbol;
  every fix confirmed to make that link succeed
  ([../results/v7.3/fix-verification.json](../results/v7.3/fix-verification.json))
- authored as `InstructorZer0 <jude.gyimah87@gmail.com>`
- each message credits the checker that found the bug

## Sending one group

```bash
cd ~/src/linux
git checkout --detach v7.3
git am ~/kreach/outgoing/rockchip/000[1-9]-*.patch
git rebase --exec 'git commit --amend --no-edit -s' v7.3   # sign off each
git format-patch -5 --cover-letter -o ~/kreach/outgoing/rockchip

# format-patch rewrites the cover letter back to *** SUBJECT HERE ***.
# Put the subject and blurb back; this does not touch the signed patches:
~/venv/bin/python ~/kreach/tools/prepare_patches.py \
    ~/src/linux ~/kreach/results/v7.3 ~/kreach/outgoing --cover-only
```

Check it before sending — `git send-email` refuses a template subject, which
is a good last line of defence but a late one:

```bash
head -4 ~/kreach/outgoing/rockchip/0000-cover-letter.patch
```

Then send with the recipients `get_maintainer.pl` gives for that file —
maintainers in `--to`, lists and wider reviewers in `--cc`:

| group | `--to` | `--cc` |
|---|---|---|
| rockchip | Girdwood, Brown, Kysela, Iwai, Stuebner | linux-sound, linux-arm-kernel, LKML |
| fsl | Girdwood, Brown, Kysela, Iwai, Liu | linux-sound, LKML |
| intel | Rojewski, Girdwood, Ujfalusi, Liao, Vehmanen, Bossart, Brown | linux-sound, LKML |
| sunxi | Girdwood, Brown, Kysela, Iwai, Tsai, Skrabec, Holland | linux-sound, linux-arm-kernel, LKML |
| drm/xe | Brost, Hellström, Vivi | Airlie, Vetter, intel-xe, dri-devel, LKML |

`git send-email` has no transport configured in that tree, so it needs
`sendemail.smtpserver` set up first.

## Order

Send **drm/xe first**: its fix is the idiom its own stanza already uses three
times, so there is nothing to argue about, and it is one patch in one file.

Then `intel/` and `fsl/0003` (the two `BITREVERSE` ones) — the neighbouring
`SND_SOC_FSL_SPDIF` already does `select BITREVERSE`, which is the same
"matches local practice" argument.

Then `rockchip/` as a series, then `sunxi/`.

Leave **`fsl/0001`** (`fsl_aud2htx`) last. It is the only patch adding two
selects, because its configuration fails on a second symbol as well, and it
is the one most likely to draw questions.

## Signed

All eleven are applied, signed and checkpatch clean:

```
From: Jude Gyimah <jude.gyimah87@gmail.com>
Signed-off-by: Jude Gyimah <jude.gyimah87@gmail.com>
```

Author and signer match deliberately. They did not at first — the patches
were authored as `InstructorZer0` while the sign-off came from the tree's
configured identity, and checkpatch flags that:

```
WARNING: From:/Signed-off-by: email name mismatch
```

A patch whose author and signer look like two people invites the question of
who is actually certifying it, so `prepare_patches.py` now takes the author
from `user.name`/`user.email` in the kernel tree and refuses to run if they
are unset.

## Provenance

Every commit message credits the checker, above the `Fixes:` tag:

```
Found with kreach, a static check that compares what Kconfig allows
against the symbols the compiled objects actually reference.
```

This follows kernel practice rather than hiding from it — `kismet` findings
and `kernel test robot` reports both name the tool that found them.

The repository link is kept **out** of the commit message and carried below
the `---` instead, where `git am` drops it:

```
The checker is at https://github.com/JGyimah/kreach, and the
reproducer configuration for each finding is published with it
under results/.
```

Below `---` is the right place for it. Everything above lives in git history
forever, and a URL to a personal repository is useful to a reviewer today and
dead weight in `git log` in five years. It is also not a `Link:` tag:
`Link:` conventionally points at a mailing-list archive or a bug, not a tool
homepage.

One note per posting — the cover letter for a series, the patch itself for a
singleton. Six copies of the same URL in a five-patch series is noise.
Verified that the note does not reach the commit: applying a patch with
`git am` and reading `git log -1` shows the credit sentence and no URL.

Regenerate it with `--note-only`, or `--cover-only` for the cover letters;
both are idempotent.
