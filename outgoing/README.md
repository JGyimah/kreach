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

## Sending one group

```bash
cd ~/src/linux
git checkout --detach v7.3
git am ~/kreach/outgoing/rockchip/000[1-9]-*.patch
git rebase --exec 'git commit --amend --no-edit -s' v7.3   # sign off each
git format-patch -5 --cover-letter -o ~/kreach/outgoing/rockchip
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

## One thing to decide

None of these messages says a tool found the bug. That is not unusual — most
kernel patches describe the bug rather than the hunt — but kernel practice
does run toward crediting the finder, and `kismet` findings routinely land
with the tool named. If you want that, add a line such as

```
Found with a static reachability check over Kconfig and the linked objects.
```

above the `Fixes:` tag. It is your call, and worth making deliberately rather
than by omission.
