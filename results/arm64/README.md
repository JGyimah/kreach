# Findings at v7.3, arm64

A kreach run on Linux **v7.3**, `ARCH=arm64` (cross-compiled, gcc 13.3), with
`sound/` as the caller subsystem. **Thirteen findings**, each confirmed by a
`vmlinux` link that failed on the predicted symbol.

The point of a second architecture was to reach code x86_64 cannot compile,
and it did: **five of the thirteen are new**, three of them Amlogic drivers
gated on `ARCH_MESON` with no `COMPILE_TEST` escape.

| | |
|---|---|
| satisfiable queries | 38 of 8,062 |
| settled by the reference pre-check, no build spent | 15 |
| builds spent | 23 |
| **confirmed** | **13** |
| rejected: the build linked cleanly | 10 |
| precision over builds spent | 13/23 = 0.57 |

## New on arm64

These five could not be found on x86_64. The three Amlogic drivers are the
reason the architecture limitation in [docs/design.md](../../docs/design.md)
mattered rather than being a formality.

| caller | provider | symbol |
|---|---|---|
| `SND_MESON_AXG_PDM` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_MESON_AXG_SPDIFIN` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_MESON_AXG_SPDIFOUT` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` |
| `SND_AW2` | `SND_PCM` | `snd_pcm_new` |
| `SND_SOC_SOF` | `SND_INTEL_DSP_CONFIG` | `intel_nhlt_get_endpoint_blob` |

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

## Files

| | |
|---|---|
| `findings.txt` | the report, one section per caller option |
| `findings.jsonl` | one record per finding, for scripts |
| `summary.json` | counts by case and fix pattern, plus per-stage timings |
| `table.md` | the findings as a table, regenerable |
| `witness/` | the minimised `.config` for each finding |
| `build/` | the `vmlinux` link log for each finding |
