# Findings at v7.3

A kreach run on Linux **v7.3** — the newest release kmax 4.10 can parse — with
`ARCH=x86_64` and `sound/` as the caller subsystem. **Ten findings, 10 of 10
builds spent confirmed**, each by a `vmlinux` link that failed on the predicted
symbol.

These supersede [`../`](..), which holds the same set at v6.17. Quote these if
you are sending a patch: a maintainer applies to something near the tip, not to
a release two cycles back.

| caller option | provider | symbol | witness | settings | link | `Fixes:` |
|---|---|---|---|---|---|---|
| `CONFIG_SND_SOC_FSL_XCVR` | `CONFIG_GENERIC_BITREVERSE` | `byte_rev_table` | [`644371d1b007.config`](witness/644371d1b007.config) | 1078 | 388 s | `28564486866f` |
| `CONFIG_SND_SOC_INTEL_KEEMBAY` | `CONFIG_GENERIC_BITREVERSE` | `byte_rev_table` | [`ba025badfe60.config`](witness/ba025badfe60.config) | 696 | 357 s | `c5477e966728` |
| `CONFIG_SND_SOC_FSL_AUD2HTX` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`6d3bbbf14c84.config`](witness/6d3bbbf14c84.config) | 700 | 355 s | `8a24c834c053` |
| `CONFIG_SND_SOC_FSL_LPC3XXX` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`277a3340a839.config`](witness/277a3340a839.config) | 505 | 168 s | `0959de657a10` |
| `CONFIG_SND_SOC_ROCKCHIP_I2S` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`3c5ec3ae665b.config`](witness/3c5ec3ae665b.config) | 841 | 464 s | `4495c89fcf26` |
| `CONFIG_SND_SOC_ROCKCHIP_I2S_TDM` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`2b48cf8a3dbd.config`](witness/2b48cf8a3dbd.config) | 506 | 180 s | `081068fd6414` |
| `CONFIG_SND_SOC_ROCKCHIP_PDM` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`ad2f3dd9f157.config`](witness/ad2f3dd9f157.config) | 828 | 478 s | `fc05a5b22253` |
| `CONFIG_SND_SOC_ROCKCHIP_SAI` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`69d816711f7b.config`](witness/69d816711f7b.config) | 511 | 181 s | `cc78d1eaabad` |
| `CONFIG_SND_SOC_ROCKCHIP_SPDIF` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`ab3a8b918f35.config`](witness/ab3a8b918f35.config) | 842 | 519 s | `f874b80e1571` |
| `CONFIG_SND_SUN50I_DMIC` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`706acba2ec1c.config`](witness/706acba2ec1c.config) | 512 | 160 s | `9fc2c8ed923d` |

One of the eleven findings from v6.17 is **not** here.
`CONFIG_SND_SOC_IDT821034` no longer reproduces: its Kconfig stanza is still
missing the `select`, so reading the source says the bug is live, but the
caller no longer emits the reference and the link cannot fail on it. Reading a
declaration tells you whether anyone fixed the rule; only compiling the caller
tells you whether the call is still there.

## The fix to send

`select <PROVIDER>` in the caller's Kconfig stanza. Each finding's `fix` field
says `depends on` instead, because the specification kreach implements rules
out suggesting `select`, and every record carries a `caveat` explaining why
that advice cannot be applied here: neither provider has a Kconfig prompt, so
a `depends on` would leave the driver unselectable.

For the two `byte_rev_table` findings, send **`select BITREVERSE`**, not
`select GENERIC_BITREVERSE`. v7.3 gates `bitrev.o` on the latter, which is why
it appears as the provider, but `BITREVERSE` is the symbol a driver selects:

```
config BITREVERSE
        tristate
        select GENERIC_BITREVERSE if !HAVE_ARCH_BITREVERSE
```

That does the right thing on arm64 and riscv, where a native implementation
means no `bitrev.o` is needed at all.

## Reproducing one

```bash
cp results/v7.3/witness/644371d1b007.config <builddir>/.config
make O=<builddir> olddefconfig
make O=<builddir> -j$(nproc) vmlinux
```

Each witness is minimised and produced by the kernel's own Kconfig, so an
unmodified `make` reproduces the failure. The `settings` column is how many
options each one enables — worth a glance before you start, since the largest
needs appreciably more memory and disk than the smallest.

## Files

| | |
|---|---|
| `findings.txt` | the report, one section per caller option, with a commit-message block |
| `findings.jsonl` | one record per finding, for scripts |
| `summary.json` | counts by case and fix pattern, plus per-stage timings |
| `table.md` | the table above, regenerable |
| `witness/` | the minimised `.config` for each finding |
