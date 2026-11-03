# Historical recall

Cases from the mined benchmark, run the way the protocol asks: check out the
**parent** of a commit that fixed a reachability bug, run the pipeline there,
and see whether kreach reports the pair the fix addressed.

**14 hits of 33 usable cases — recall 0.42.** The whole mined benchmark was
run: 120 commits since v6.0, 59 with an extractable caller/provider pair, 26
excluded as not a y/m instance, 33 scored. `tools/bench_report.py --triage`
reproduces the table below from the saved per-case diagnostics.

| | |
|---|---|
| hits | **14** — 11 on the exact pair, 2 on symbol and provider, 1 on symbol and caller |
| misses | 19 |
| excluded | 26 |
| recall over scored | **0.42** |

### What stopped the 19 misses

| cause | n | whose problem |
|---|---|---|
| K says the y/m case is unreachable | 4 | kreach's model, or correctly so — undiagnosed individually |
| witness built, caller does not emit the reference | 4 | the reference depends on an option the query does not constrain (S4's approximated RC) |
| satisfiable, but Kconfig rejected the witness | 3 | **the S1 conditional-default gap**, already the documented next task |
| candidates formed, none against a provider the fix names | 3 | the two sides name the pair differently |
| no candidate pair formed at all | 2 | the caller was not built in the scoped tree |
| kreach confirmed a finding the scorer did not match | 1 | a *different*, real bug in the same driver |
| build failed for an unrelated reason | 1 | environment |
| no diagnostics saved | 1 | `fsl_sai`, diagnosed by hand: x86_64 cannot see it |

Four were diagnosed in full and none was a plain checker failure. That is the
honest frame for 0.42: it is a lower bound on a benchmark that is still partly
measuring itself, not a statement that kreach finds two bugs in five.

One miss is worth its own line. On `2182f358fb13` kreach confirmed a y/m bug
in `drivers/gpu/drm/xe` — `xe_module.o` against `xe_configfs.o` — that is **not
the bug the commit fixed and is not in the benchmark at all**. It is scored as
a miss, correctly, and it is a finding nobody asked for.

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

A case is excluded when the pair the miner extracted cannot be a y/m instance
at the parent commit. Three shapes, each verified by hand on the case that
first produced it, and each a statement about the mining or the architecture
rather than about the checker:

| rule | why it cannot be y/m | first seen |
|---|---|---|
| the caller is a tristate that already `depends on` the provider | Kconfig caps a symbol at the level of what it depends on, so `y` with the provider at `m` is unreachable | `1db63f6af179` `SND_SOC_RT5575_SPI` |
| no provider the fix names can be a module | only a tristate has an `m`; a `bool` provider means the bug is the **y/n** case, which v1 does not check | `10006ad04b74`, whose provider `NET_VENDOR_FREESCALE` is a menu gate |
| the caller is gated on another architecture with no `COMPILE_TEST` | the caller is unselectable here, so S3 never compiles it and no reference of its is ever seen | `1274045b0eda` `SCSI_IBMVFC`, `depends on PPC_PSERIES` |

Over 120 mined commits, 59 cases have an extractable caller/provider pair and
**22 are excluded: 16 for a bool provider, 4 for another architecture, 2 for a
capped caller.** That leaves **37 usable**, and it reframes the benchmark: most
of what it had been offering was the y/n case, which kreach v1 states as out
of scope. Verdicts recorded before these rules existed are in
[superseded/](superseded/).

### The rule that started it


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

## What counts as a hit

The obvious test is whether kreach reports the `(caller option, provider
option)` pair the fix addressed, and that is the one to prefer. But the two
sides do not always name the caller the same way. A fix edits the stanza a
human thinks of as the driver — often a `bool`, such as `MLX5_MACSEC` — while
kreach names the option whose *level* decides whether the object reaches
`vmlinux`, the tristate on the `obj-` line. Both are correct about different
things, and an exact-pair test scores the same bug as a miss.

So a second form of hit is allowed, recorded as `matched: "symbol and
provider"`: the same **symbol**, from one of the providers the fix names,
reported against a caller that is one of the files referencing that symbol at
the parent commit. The symbol is what keeps it tight — it pins the finding to
the bug rather than to the class — and the last clause is what stops a
different driver's bug on the same symbol being credited. Scoring without a
kernel tree to check that clause against falls back to exact pairs only.

## A rule that cannot be written

`514b2262ade4` ("firmware: arm_scmi: Fix i.MX build dependency") missed, and
the obvious reading was that the miner had named a stanza that is neither side
of the pair: `IMX_SCMI_MISC_EXT` gates
`drivers/firmware/arm_scmi/vendors/imx/imx-sm-misc.o`, while the exported
`scmi_imx_misc_ctrl_get`/`_set` come from `drivers/firmware/imx/sm-misc.o`
under `IMX_SCMI_MISC_DRV`, and the only genuine caller is
`sound/soc/fsl/fsl_mqs.c` under `SND_SOC_FSL_MQS`. That suggests a mining
rule: exclude a case when no object the named caller gates actually
references the named symbols.

It cannot be decided at mining time. `imx-sm-misc.c` contains

```
static int scmi_imx_misc_ctrl_get(const struct scmi_protocol_handle *ph,
                                  u32 ctrl_id, u32 *num, u32 *val)
```

-- a file-local static with the same name as the exported symbol. Neither a
substring grep nor a word-boundary grep can tell that from a reference to the
exported one; only the compiled object's symbol table can, which is precisely
what S3 and S4 already compute. A grep-based rule would also be wrong in the
other direction: it would exclude `93b5d21e8b5c`, where the caller option
appears nowhere near the referencing object's Makefile and the case is a
genuine limitation worth counting as a miss.

So this shape stays a miss, with the reason recorded here rather than encoded
as an exclusion. The useful part of looking was finding that both symbol
lookups used a substring grep where they meant whole words.

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
