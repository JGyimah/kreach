# Findings

A kreach run on Linux **v6.17**, `ARCH=x86_64`, with `sound/` as the caller
subsystem. Eleven findings, every one confirmed by a real `vmlinux` link that
failed on the predicted symbol. [docs/results.md](../docs/results.md) has the
stage-by-stage numbers and what the run cost.

**Superseded by [v7.3/](v7.3/)**, where the same set was re-run two releases
on: **10 of these 11 still reproduce**, 10 of 10 builds spent. Send those, not
these — a maintainer applies to something near the tip.

`SND_SOC_IDT821034` is the one that drops out. Its Kconfig stanza is still
missing the `select`, but the caller no longer emits the reference, so the link
cannot fail on it. Quote a v7.3 reproducer, not one of these, if you are sending a
patch; see [docs/results.md](../docs/results.md).

All eleven are the `y/m` case the spec calls the main target: the caller is
built into vmlinux, the provider is a module, and nothing in Kconfig stops that
combination.

| caller option | provider | symbol | witness | link | Fixes: |
|---|---|---|---|---|---|
| `CONFIG_SND_SOC_FSL_XCVR` | `CONFIG_BITREVERSE` | `byte_rev_table` | [`70375580a27d.config`](witness/70375580a27d.config) | 21 s | `28564486866f` |
| `CONFIG_SND_SOC_IDT821034` | `CONFIG_BITREVERSE` | `byte_rev_table` | [`d9d629e6b5ad.config`](witness/d9d629e6b5ad.config) | 118 s | `e51166990e81` |
| `CONFIG_SND_SOC_INTEL_KEEMBAY` | `CONFIG_BITREVERSE` | `byte_rev_table` | [`6b4e800877e7.config`](witness/6b4e800877e7.config) | 24 s | `c5477e966728` |
| `CONFIG_SND_SOC_FSL_AUD2HTX` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`6d3bbbf14c84.config`](witness/6d3bbbf14c84.config) | 252 s | `8a24c834c053` |
| `CONFIG_SND_SOC_FSL_LPC3XXX` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`277a3340a839.config`](witness/277a3340a839.config) | 544 s | `0959de657a10` |
| `CONFIG_SND_SOC_ROCKCHIP_I2S` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`3c5ec3ae665b.config`](witness/3c5ec3ae665b.config) | 208 s | `4495c89fcf26` |
| `CONFIG_SND_SOC_ROCKCHIP_I2S_TDM` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`2b48cf8a3dbd.config`](witness/2b48cf8a3dbd.config) | 491 s | `081068fd6414` |
| `CONFIG_SND_SOC_ROCKCHIP_PDM` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`ad2f3dd9f157.config`](witness/ad2f3dd9f157.config) | 142 s | `fc05a5b22253` |
| `CONFIG_SND_SOC_ROCKCHIP_SAI` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`69d816711f7b.config`](witness/69d816711f7b.config) | 755 s | `cc78d1eaabad` |
| `CONFIG_SND_SOC_ROCKCHIP_SPDIF` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`ab3a8b918f35.config`](witness/ab3a8b918f35.config) | 54 s | `f874b80e1571` |
| `CONFIG_SND_SUN50I_DMIC` | `CONFIG_REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | [`706acba2ec1c.config`](witness/706acba2ec1c.config) | 368 s | `9fc2c8ed923d` |

Each `Fixes:` commit is the one that added the driver, which restates the
finding: these have been missing the dependency since they landed.

## Reproducing one

```bash
cp results/witness/70375580a27d.config <builddir>/.config
make O=<builddir> olddefconfig
make O=<builddir> -j$(nproc) vmlinux
```

The link fails with `undefined reference to 'byte_rev_table'`. Each witness is
a minimised configuration produced by the kernel's own Kconfig
(`make KCONFIG_ALLCONFIG=<fragment> allnoconfig`), so an unmodified `make`
reproduces the failure.

## A note on the suggested fix

Every finding's `fix` field says `depends on <PROVIDER>`, because the spec
kreach implements rules out suggesting `select`. For these eleven that advice
cannot be applied as written, and each record carries a `caveat` saying so:
`BITREVERSE` and `REGMAP_MMIO` have no Kconfig prompt, so they are only ever
enabled by a `select`, and a `depends on` would leave the driver unselectable.

The fix a maintainer would apply is `select`, which is what the neighbouring
`SND_SOC_FSL_SPDIF` already does for `BITREVERSE`. The rule is well motivated
in general — `select` on a symbol with its own dependencies is how
unmet-dependency bugs get created — but it does not hold for a dependency-free
helper.

## Files

| | |
|---|---|
| `findings.txt` | the report, one section per caller option, with a commit-message block |
| `findings.jsonl` | one record per finding, for scripts |
| `summary.json` | counts by case and fix pattern, plus per-stage timings |
| `witness/` | the minimised `.config` for each finding |
