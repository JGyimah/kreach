# Superseded verdicts

These four cases were scored before the miner learned to recognise pairs that
cannot be a y/m instance. Each was recorded as a miss; each is now excluded,
for a reason that has nothing to do with the checker:

| case | why it is excluded now |
|---|---|
| `1274045b0eda` `SCSI_IBMVFC` | `depends on PPC_PSERIES`, declared only under `arch/powerpc/`, no `COMPILE_TEST` — an x86_64 run cannot build the caller |
| `5f1e4f65d300` `AMLOGIC_THERMAL` | `depends on ARCH_MESON`, declared only under `arch/arm/` and `arch/arm64/` — same |
| `10006ad04b74` `NET_DSA_NETC_SWITCH` | the only provider the fix names, `NET_VENDOR_FREESCALE`, is a `bool` menu gate: it cannot be a module, so there is no y/m case |
| `1db63f6af179` `SND_SOC_RT5575_SPI` | a tristate already `depends on SPI_MASTER` at the parent, so it cannot be `y` while that is `m` |

A fifth was set aside for a different reason.

| case | why its verdict was withdrawn |
|---|---|
| `87ab8276ed24` `MLX5_MACSEC` | the sweep asked S5 only about the caller option the fix edited, and kreach attributes this caller to `MLX5_CORE` -- the tristate on the `obj-` line, whose level decides whether the object reaches `vmlinux` -- not to the bool `MLX5_MACSEC` that merely gates it. The filter matched no candidates, so the solver was never asked. The recorded miss was the harness, not a verdict. |

That is what the sweep's provider fallback exists for, and this case is being
re-run with it.

These files are kept rather than deleted because the verdict files are the
record of what was run, and because four of the five are the evidence for a
rule or a fix added after them. They are **not** part of the aggregate:
summing `../score-*.json` is the right figure, and these are deliberately
outside it.
