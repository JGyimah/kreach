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

They are kept rather than deleted because the verdict files are the record of
what was run, and because the first three are the evidence for the two mining
rules added after them. They are **not** part of the aggregate: summing
`../score-*.json` is the right figure, and these are deliberately outside it.
