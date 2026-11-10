# Findings at v7.3, arm64

A kreach run on Linux **v7.3**, `ARCH=arm64` (cross-compiled, gcc 13.3), with
`sound/` as the caller subsystem. **Fourteen findings**, each confirmed by a
`vmlinux` link that failed on the predicted symbol.

**Six of the fourteen the x86_64 sweep did not confirm** — but not because
x86_64 cannot reach them. See [New on arm64](#new-on-arm64) for what actually
separates the two runs; the short version is that x86_64 can only reach these
drivers through `COMPILE_TEST`, and those configurations are too degenerate to
trigger the bug.

| | |
|---|---|
| satisfiable queries | 38 of 8,062 |
| settled by the reference pre-check, no build spent | 14 |
| builds spent | 24 |
| **confirmed** | **14** |
| rejected: the build linked cleanly | 10 |
| precision over builds spent | 14/24 = 0.58 |

## New on arm64

These six are the ones the x86_64 sweep did not confirm. The earlier wording
here said they were unreachable from x86_64; that was wrong, and what is
actually going on is more interesting.

The Amlogic menu reads

```
menu "Amlogic"
	depends on ARCH_MESON || (COMPILE_TEST && COMMON_CLK)
```

so there *is* a `COMPILE_TEST` escape, and the x86_64 run used it: all four
meson pairs were raised and satisfiable there, and it built all four. None
confirmed — one linked cleanly, two were stopped by the reference pre-check,
and one died on a missing `autoconf.h`.

The reason is in the witnesses. x86_64 reaches these drivers only through
`COMPILE_TEST`, and its witnesses carry `CONFIG_OF=n` and
`CONFIG_RESET_CONTROLLER=n`. In that configuration the probe bodies are dead
code before they ever reach the register map: `axg-pdm.c:597` returns early
because `of_device_get_match_data()` is an `OF=n` stub that always yields
`NULL`, and `aiu.c` returns early at `device_reset()`, a stub that always
fails. The call to `devm_regmap_init_mmio()` is never compiled, so no link can
fail on it. The native arm64 witnesses have `OF=y` and emit the reference.

`SND_AW2` and `SND_SOC_SOF` are a plainer story: both were satisfiable on
x86_64 and simply never built. That sweep spent 17 builds on 55 satisfiable
queries.

So what the second architecture bought here is not access to unreachable
code. It is that a `COMPILE_TEST` configuration is a poor witness for a driver
whose probe is guarded by platform stubs, while a native one is not.

| caller | provider | symbol |
|---|---|---|
| `SND_MESON_AIU` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_MESON_AXG_PDM` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_MESON_AXG_SPDIFIN` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_MESON_AXG_SPDIFOUT` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_AW2` | `SND_PCM` | `snd_pcm_new` |
| `SND_SOC_SOF` | `SND_INTEL_DSP_CONFIG` | `intel_nhlt_get_endpoint_blob` |

## The one finding of a different shape

Thirteen of the fourteen are a missing `select`. `SND_SOC_SOF` →
`SND_INTEL_DSP_CONFIG` on `intel_nhlt_get_endpoint_blob` is not, and the
guard involved is worth looking at: it exists, and it cannot work.

Both `include/sound/intel-nhlt.h:127` and the call site at
`sound/soc/sof/ipc4-topology.c:1793` are guarded by

```c
#if IS_ENABLED(CONFIG_ACPI) && IS_ENABLED(CONFIG_SND_INTEL_NHLT)
```

which tests a *different option* from the one that ships the code:

- `intel-nhlt.o` is a part of the composite `snd-intel-dspcfg`, whose
  tristate is `SND_INTEL_DSP_CONFIG` — `sound/hda/core/Makefile:18` reads
  `snd-intel-dspcfg-$(CONFIG_SND_INTEL_NHLT) += intel-nhlt.o`.
- `SND_INTEL_NHLT` is a `bool`, and `SND_INTEL_DSP_CONFIG` does
  `select SND_INTEL_NHLT if ACPI`. A bool selected from an `m`-valued symbol
  becomes `y`.

So `SND_INTEL_NHLT=y` whenever the provider is `y` *or* `m`. The condition
separates "NHLT support is present somewhere" from "absent" and nothing
else — it is structurally incapable of catching the `y/m` case, and the
`static inline` fallbacks behind it are unreachable for the same reason. The
witness has `CONFIG_SND_INTEL_DSP_CONFIG=m` with `CONFIG_SND_INTEL_NHLT=y`,
which is exactly that.

The configuration arrives there through two vendors disagreeing.
`SND_SOC_SOF_IMX_COMMON=y` selects `SND_SOC_SOF`, putting the generic core in
vmlinux, while `SND_SOC_SOF_INTEL_COMMON=m` makes `SND_INTEL_DSP_CONFIG` a
module. The Kconfig help on `SND_SOC_SOF` says the built-in or module type
"is decided at the top level [so] it doesn't matter"; here there are two top
levels. Intel SOF selects the provider itself, so the bug requires a
*non-Intel* SOF built in — the odd-looking witness is forced by the bug, not
an artefact of minimisation.

**This pattern has been fixed in this subsystem before.** `8a3fea95fab1`
("ASoC: SOF: SND_INTEL_DSP_CONFIG dependency") hit it on
`snd_intel_dsp_driver_probe` in the generic `sof-pci-dev.c` and wrapped the
call in `IS_REACHABLE(CONFIG_SND_INTEL_DSP_CONFIG)`, describing that as a
temporary fix pending a restructure. `194fe0fc3422` ("ASoC: SOF: pci: move
DSP_CONFIG use to platform-specific drivers") then did the restructure,
moving the call into `sound/soc/sof/intel/hda.c`. The NHLT calls in
`ipc4-topology.c` are what that move left behind.

There is therefore no `select` to add, which is why this one was reported to
the SOF maintainers on 2026-11-07 rather than patched. The report offers two
remedies: the one-line `IS_REACHABLE` change at `ipc4-topology.c:1793`,
which needs no header change because the file already stubs
`snd_sof_get_nhlt_endpoint_data()` in its own `#else` at line 1972; or
completing the move, for which the evidence is that `ipc4_data->nhlt` is a
`void *` in the generic `ipc4-priv.h` and is filled only by
`intel/hda-dai.c:764`. Reproducer:
[`witness/031032ef6141.config`](witness/031032ef6141.config); link log:
[`build/031032ef6141.log`](build/031032ef6141.log).

## Upstream

**Twelve of the fourteen are fixed upstream.** The four Amlogic drivers went
in from this run, on the second posting — the first was respun because patch
2/4's description referred to "these three" with nothing naming three, a
sentence that predated the `aiu` finding and whose antecedent was only in the
cover letter, which `git am` drops:

| patch | commit |
|---|---|
| `ASoC: meson: aiu: select REGMAP_MMIO` | [`cc2f13940cee`](https://git.kernel.org/broonie/sound/c/cc2f13940cee) |
| `ASoC: meson: axg-pdm: select REGMAP_MMIO` | [`12d753e6ca1d`](https://git.kernel.org/broonie/sound/c/12d753e6ca1d) |
| `ASoC: meson: axg-spdifin: select REGMAP_MMIO` | [`4603b51f1dad`](https://git.kernel.org/broonie/sound/c/4603b51f1dad) |
| `ASoC: meson: axg-spdifout: select REGMAP_MMIO` | [`10ac156211d2`](https://git.kernel.org/broonie/sound/c/10ac156211d2) |

All four carry `Reviewed-by:` from Jerome Brunet and Valerio Setti. The `aiu`
one is the finding S6 had discarded as `skipped_reference_not_emitted`,
recovered only when the reference check was taught to look at every object an
option gates — see
[The gx-formatter case](#the-one-finding-of-a-different-shape) below for the
mechanism and `kreach/s6_confirm.py` for the fix.

The other eight are the ones that reproduce on x86_64 too, and their fixes
landed from that run: five Rockchip drivers in `for-7.4` and
`fsl_aud2htx`, `fsl: lpc3xxx-i2s` and `sunxi: sun50i-dmic` in `for-7.5`.
They are listed with their commits in
[../v7.3/README.md](../v7.3/README.md).

Still outstanding: `SND_AW2`, which has not been sent, and `SND_SOC_SOF`,
which was reported rather than patched for the reason below.

## Confirmed on both architectures

Eight findings reproduce here as well as on x86_64, including all five
Rockchip drivers already applied to `broonie/sound.git for-7.4`:

`SND_SOC_ROCKCHIP_I2S`, `SND_SOC_ROCKCHIP_I2S_TDM`, `SND_SOC_ROCKCHIP_PDM`,
`SND_SOC_ROCKCHIP_SAI`, `SND_SOC_ROCKCHIP_SPDIF`, `SND_SOC_FSL_AUD2HTX`,
`SND_SOC_FSL_LPC3XXX`, `SND_SUN50I_DMIC`.

That is worth more than a duplicate count. On x86_64 those drivers only
compile because `COMPILE_TEST` pulls them into a build they were not written
for, which a maintainer could fairly dismiss as artificial. Here they are
built for the architecture they target, and the link fails anyway.

## Not found here

`SND_SOC_FSL_XCVR` and `SND_SOC_INTEL_KEEMBAY`, both `byte_rev_table` cases.
arm64 selects `HAVE_ARCH_BITREVERSE`, so `GENERIC_BITREVERSE` does not gate
`bitrev.o` there and the y/m pair does not arise — which is the same
architecture-native-implementation argument the fix for those two rests on.
Checked, not assumed.

## The model travelled

Before trusting any of this, the S1 acceptance test was run for the new
architecture: **4 of 8 randconfigs satisfy K**, identical to the x86_64
baseline. The encoding and the six repairs are not x86-specific. Direction 2
is unchanged at 0 of 7, drifting 65 to 279 symbols — the same conditional-
default gap, no new arm64-specific problem.

Of the symbols that drift, `ACPI_LEGACY_TABLES_LOOKUP`,
`ARCH_MIGHT_HAVE_ACPI_PDC` and a run of `ARMADA_*_CLK` recur. All are
promptless and computed by Kconfig, which is the class tristate-valued
default evaluation would fix, so arm64 supplies more evidence for that work
rather than complicating it.

One recall caveat specific to cross-building: R4 probes toolchain-dependent
symbols against the host and found **100 unavailable** here against 14 on
x86_64. Those are pinned off, so some arm64 code is not considered reachable.
That costs recall, not precision.

## The fix is verified, not just reasoned

With the `select` added, **thirteen of the fourteen witnesses link** — 111 s
to 269 s each — and with all thirteen applied at once `allnoconfig`,
`defconfig`, `allmodconfig` and `allyesconfig` come back Kconfig-clean. The
fourteenth, `SND_SOC_SOF`, was not attempted: it has no mechanical `select`
to add, for the reason set out in
[The one finding of a different shape](#the-one-finding-of-a-different-shape).
See
[`fix-verification.json`](fix-verification.json), produced by
[tools/verify_fix.py](../../tools/verify_fix.py).

The one check still outstanding is the same one as on x86_64: an
`allmodconfig` `vmlinux` link, which wants about 20 GB of object tree
against the 4.8 GB free here.

## Files

| | |
|---|---|
| `findings.txt` | the report, one section per caller option |
| `fix-verification.json` | each fix applied and rebuilt, plus the stock-config regression |
| `findings.jsonl` | one record per finding, for scripts |
| `summary.json` | counts by case and fix pattern, plus per-stage timings |
| `table.md` | the findings as a table, regenerable |
| `witness/` | the minimised `.config` for each finding |
| `build/` | the `vmlinux` link log for each finding |
