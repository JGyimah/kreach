# Ready to send — the arm64 findings

Four patches from the arm64 run, signed and `checkpatch` clean. The other
nine arm64 findings are **not** here: eight duplicate findings already applied
or in flight from the x86_64 run, and sending those again would be wrong —
applied patches are not replaced. The ninth needs a judgement I should not
make for you; see below.

| group | patches | file |
|---|---|---|
| [`meson/`](meson/) | 3 + cover letter | `sound/soc/meson/Kconfig` |
| [`pci/`](pci/) | 1 | `sound/pci/Kconfig` |

Each bug is confirmed by a `vmlinux` link that failed on the named symbol,
cross-compiled for arm64 at v7.3. The reproducers and link logs are in
[../results/arm64/](../results/arm64/).

## meson — three Amlogic drivers

`SND_MESON_AXG_PDM`, `_SPDIFIN` and `_SPDIFOUT` call
`__devm_regmap_init_mmio_clk` and none selects `REGMAP_MMIO`. Several stanzas
in the same file already do, `SND_MESON_G12A_TOACODEC` among them, so each
patch leads with that.

These three are the ones x86_64 could never have found: they are gated on
`ARCH_MESON` with no `COMPILE_TEST` escape.

```bash
cd ~/src/linux && git send-email \
  --to=jbrunet@baylibre.com --to=lgirdwood@gmail.com --to=broonie@kernel.org \
  --to=perex@perex.cz --to=tiwai@suse.com \
  --cc=neil.armstrong@linaro.org --cc=khilman@baylibre.com \
  --cc=martin.blumenstingl@googlemail.com \
  --cc=linux-sound@vger.kernel.org \
  --cc=linux-amlogic@lists.infradead.org \
  --cc=linux-arm-kernel@lists.infradead.org \
  --cc=linux-kernel@vger.kernel.org \
  ~/kreach/outgoing-arm64/meson/0*.patch
```

## pci — SND_AW2

`aw2-alsa` calls `snd_pcm_new` and `SND_AW2` selects nothing at all, while
thirty-six other stanzas in the same file select `SND_PCM`.

```bash
cd ~/src/linux && git send-email \
  --to=perex@perex.cz --to=tiwai@suse.com \
  --cc=linux-sound@vger.kernel.org --cc=linux-kernel@vger.kernel.org \
  ~/kreach/outgoing-arm64/pci/0001-*.patch
```

`get_maintainer.pl` lists several recent committers to `sound/pci/Kconfig`
who are not maintainers of it — Petkov, Golaszewski, Torokhov, Walleij,
Bergmann. They are left out deliberately; CCing everyone who touched a file
is noise.

## The one without a patch

`SND_SOC_SOF` → `SND_INTEL_DSP_CONFIG` on `intel_nhlt_get_endpoint_blob` is a
real finding of the usual shape. `include/sound/intel-nhlt.h` guards the
symbol with `#if IS_ENABLED(CONFIG_ACPI) && IS_ENABLED(CONFIG_SND_INTEL_NHLT)`
and supplies a `static inline` stub otherwise, which protects the `n` case and
not the module case, because `IS_ENABLED` is true for `m`.

No patch, because the fix is a layering decision rather than a mechanical one.
`SND_SOC_SOF` is the generic SOF core — promptless, selected from above — and
`SND_INTEL_DSP_CONFIG` is Intel-specific. Having generic SOF select an Intel
symbol would be wrong; the dependency probably belongs on the Intel SOF
option, and that is the SOF maintainers' call. Worth sending as a bug report
with the reproducer from
[../results/arm64/](../results/arm64/) rather than as a patch.

## Also worth a look

The meson witness logs show undefined references from `gx-formatter.c` as
well as from the three drivers patched here. kreach did not report
`gx-formatter`'s option as a separate finding, and it is not obvious why —
possibly composite attribution. Worth checking before concluding the meson
set is complete.
