# Historical recall

Cases from the mined benchmark, run the way the protocol asks: check out the
**parent** of a commit that fixed a reachability bug, run the pipeline there,
and see whether kreach reports the pair the fix addressed.

**2 hits of 3 scored cases, 1 excluded.** Four were run — the four whose
caller option lives in `sound/`, the subsystem S3 builds. One of them turned
out not to be a y/m instance at all and the miner now excludes it rather than
scoring it as a miss; see *Excluding a case* below. These four are not a
sample of the benchmark, so 0.67 is not the benchmark's recall figure.

| case | fix | run at | result |
|---|---|---|---|
| [`999fb9d51f93`](case-999fb9d51f93/) | ASoC: qcom: sm8250 — `depends on X \|\| !X` | `86731a2a651e`, v6.16-rc3 | **hit** |
| [`882826f58b2c`](case-882826f58b2c/) | ALSA: usb-audio: qcom — USB_XHCI dependency | `e2d8ae899760`, v6.15-rc6+ | **hit** |
| `742048f2e128` | ASoC: fsl_sai — IMX_SCMI_MISC_DRV dependency | `cad9720dd7e4`, v6.19-rc7 | miss |
| `1db63f6af179` | ASoC: rt5575 — SPI dependency | `a1d14d8364ea`, v6.19-rc7 | excluded |

## The hits

`999fb9d51f93` fixed its bug with `depends on SND_SOC_QCOM_OFFLOAD_UTILS ||
!SND_SOC_QCOM_OFFLOAD_UTILS`, the `X || !X` pattern that says "if the provider
is a module I must be a module too" — the y/m case exactly. At the parent, one
of the five queries for `CONFIG_SND_SOC_SM8250` was satisfiable and the
`vmlinux` link failed in 650 s on `qcom_snd_usb_offload_jack_remove`, the
symbol the fix's own message names. kreach also reports
`qcom_snd_usb_offload_jack_setup`, which it does not.

`882826f58b2c` is the harder of the two: five providers in the mined case, and
kreach found the pair through `CONFIG_USB_XHCI_HCD`, failing the link in 514 s
on ten `xhci_sideband_*` symbols at once.

## The miss

**`742048f2e128` — the reference does not exist on this architecture.** S5
produced a satisfiable witness, and S6's pre-check then found that
`sound/soc/fsl/fsl_sai.o` does not reference `scmi_imx_misc_ctrl_set` at all.
The call is real and unguarded in the source, but it sits inside
`if (of_device_is_compatible(np, "fsl,imx952-sai") && ...)`, and with
`CONFIG_OF=n` — which is what the witness has, on x86_64 —
`of_device_is_compatible()` is a `static inline` returning false, so the
compiler deletes the whole block. The bug is real on i.MX, where `CONFIG_OF=y`;
an x86_64 run cannot see it. This is the architecture limitation in
[docs/design.md](../../docs/design.md) measured rather than asserted, and the
pre-check behaving correctly: it refused to spend a build on a reference that
is not emitted.

## Excluding a case

`1db63f6af179` was the fourth case run, and all four of its queries came back
unsat. K was right. At the parent the stanza reads:

```
config SND_SOC_RT5575_SPI
        tristate "Realtek ALC5575 Codec - SPI"
        depends on SPI_MASTER && I2C
        depends on SND_SOC_RT5575
```

A tristate with `depends on SPI_MASTER` cannot be `y` while `SPI_MASTER` is
`m` — Kconfig caps a symbol at the level of what it depends on, R6 encodes
that, and the solver is right to say no configuration satisfies the query. So
the y/m instance the miner inferred is not reachable at the parent. The fix
changed the symbol to `bool` and added `depends on SPI_MASTER=y ||
SND_SOC_RT5575=m`, which points at the bool-depends-on-tristate hazard that
only exists *after* that change.

`kreach bench` now recognises this shape and excludes such a case instead of
scoring it as a miss: if every provider the fix names is already capped by the
caller's own `depends on` at the parent, the extracted pair cannot be a y/m
instance, and counting it would measure the mining rather than the checker.
The rule is narrow on purpose, and two distinctions carry it:

- **Only a tristate caller is capped.** A `bool` has no `m` level, so
  `depends on P` leaves it `y` with P at `m` — that is the hazard kreach looks
  for, not a protection, and such a pair is kept.
- **Only a bare conjunct counts.** `depends on P=y` is an exact-level test,
  which is the *fix* for this class rather than an instance of it, and a term
  inside `||` is not required at all.

It only fires when *every* named provider is capped. `882826f58b2c` is the
case that shows why: four of its five providers are capped, but
`CONFIG_USB_XHCI_HCD` is not — and that is the pair kreach went on to confirm.
Over 120 mined commits the rule excludes two of the 59 extractable cases,
leaving 57 usable.

## Method

S5 and S6 were filtered to each case's caller option, so this answers "does
kreach confirm this pair at the parent commit", not "does a full sweep surface
it among everything else". Filtering cannot change a verdict — same query,
same build — but it skips the rest of the sweep.

About 25 minutes per case on this container, most of it S2 over the whole tree
and the wide `sound/` build. The remaining 55 usable cases mostly have callers
in `drivers/`, so each needs S3 to build its own subsystem.

[docs/results.md](../../docs/results.md) has the per-stage numbers.

## Files

| | |
|---|---|
| `score-<commit>.json` | the scorer's verdict for one case |
| `case-<commit>/` | a hit as published: report, records, witness, link log |
