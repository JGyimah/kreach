# Measured run: Linux v6.17, x86_64

Everything below was produced by the tool in this repository, on one multipass
container: Ubuntu 24.04, 4 cores, 3 GB RAM, 24 GB disk, gcc 13.3, kmax 4.10,
z3 4.15. The kernel is `v6.17` from `stable/linux.git`.

This is the spec's **Milestone 1** scope and a bit past it: S1–S3 for one
architecture with the caller side restricted to one subsystem, plus S4–S7 run
through end to end.

## What the container's size dictated

Two constraints shaped the run, and both cost recall rather than precision.

**S3 needs a wide build, and a whole-tree `allmodconfig` does not fit.** A
full `allmodconfig` object tree is tens of gigabytes; the container has 24 GB
total with a 7 GB kernel clone in it. So the wide build is `allmodconfig`
configured for the whole tree but *built* only under `sound/` — 2,644 objects,
8.2 minutes. That gives references for `sound/` callers only.

**S2 does not need a build.** kmax is static, so the presence-condition table
covers the whole tree regardless: the main code directories plus `arch/x86/`,
48,341 Kbuild paths, 32,200 objects, 14.1 minutes. Callers are limited to
`sound/`; providers are not. That distinction matters — restricting S2 to
`sound/` as well left 24,015 references with no provider presence condition;
the whole-tree table cut that to 2,025, and adding `arch/x86/` to 543.

## Stage by stage

| stage | input | result | time |
|---|---|---|---|
| S1 | Kconfig tree, `ARCH=x86_64` | 18,333 symbols (11,481 tristate, 6,500 bool); 111,355 constraints | 1.5 min |
| S2 | whole tree + `arch/x86` | 32,200 objects from 48,341 Kbuild paths; 144 symbols keep the enabled reading in Makefile conditionals; 294 objects sit in an exact-`y` block; 6 symbols use an exact-`m` test; 211 Kbuild symbols this architecture never declares; 9 unmodelled constructs | 14.1 min |
| S3 | `allmodconfig`, built under `sound/` | 2,644 objects; 80,078 references from 2,625 objects; 44,702 provided symbols; 37,305 `EXPORT_SYMBOL*` from 33,487 sources; 994 composite objects | 8.2 min build + 6 s harvest |
| S4 | the above | 12,315 candidate pairs; 679 dropped as already guarded (635 by a header `IS_REACHABLE`, 44 at the call site); 993 composite callers skipped; 71 skipped for an undeclared provider option | 6 s |
| S5 | 12,315 pairs | collapsed to 7,550 distinct queries; 7,402 unsat, 55 satisfiable with an accepted witness, 93 rejected | 1.6 h |
| S6 | satisfiable witnesses | one `vmlinux` link per witness | see below |
| S7 | confirmed findings | grouped by caller option | — |

S1 and S2 are the per-commit cost: about 14 minutes for an architecture.

## S1: the Kconfig model

The acceptance test the spec attaches to S1 — compare K against real
`make randconfig` output — failed completely on the first run: **0 of 4**
randconfigs satisfied K. Every failure came back as a one- or two-literal
unsat core, which made the causes diagnosable one at a time. Six classes
turned up; five are repaired, one is deliberately not.
[docs/design.md](design.md) has the full account; the summary:

| | what kclause gets wrong | fired |
|---|---|---|
| R1 | a `select`ed tristate may never be a module — forbids exactly what kreach looks for | 45 constraints replaced by 24,424 level-aware ones over 15,201 select pairs |
| R2 | `imply` carries no constraint, but the symbol is forced off | 166 symbols |
| R3 | a symbol declared in several Kconfig files keeps one dependency | 3 symbols |
| R4 | `$(success,...)` and friends mis-evaluated outside the kernel's make | 131 scanned, 43 handled (28 forced on by this host) |
| R5 | a non-visible selected symbol is not bounded above | 1,776 bounds — **not** in K, used for witness realisability |
| R6 | `depends on` loses the level cap, so `SND_SOC=y` with `SND=m` is admitted | 8,353 clamps |

| | first run | R1–R4 | R1–R4 + R6 |
|---|---|---|---|
| randconfigs satisfying K | 0/4 | 4/8 | **4/8** |
| R6 clamps alone rejecting a randconfig | — | — | 0/8 |
| models of K surviving `olddefconfig` unchanged | 0/4 | 0/8 | 0/10 |
| drift per model, of 18,333 symbols | 73–1,394 | 73–1,394 | **63–182** (median 123) |
| S5 witnesses Kconfig kept | — | 0/10 | **40/40** |

Direction 1 of the acceptance test went from 0/4 to 4/8. The four remaining
failures are one family — Kconfig's *value* propagation through `select` for
symbols that are also constrained another way — and getting them right needs a
tristate-valued evaluator for Kconfig expressions rather than a Boolean
encoding with level variables added, which is the non-goal the spec draws.

Direction 2 does not pass, and that is the honest state of S1. R6 cut the drift
by an order of magnitude but a model of K still differs from what
`olddefconfig` produces by a median of 123 symbols, almost all non-visible
symbols whose value Kconfig computes from defaults and reverse dependencies.
It matters to kreach only through the witness rejection rate, because witnesses
are produced by real Kconfig rather than written out from a model — and that
rate is now zero.

## S2: the acceptance test

Six configurations, 2,837 objects under `sound/` each — 17,022 object-level
checks:

| configuration | built y | built m | mismatches | composite-only |
|---|---|---|---|---|
| `allmodconfig` | 0 | 2,644 | 0 | 0 |
| `defconfig` | 60 | 0 | 0 | 14 |
| `randconfig-1` | 134 | 634 | **3** | 55 |
| `randconfig-2` | 264 | 0 | 0 | 192 |
| `randconfig-3` | 551 | 0 | 0 | 366 |
| `randconfig-4` | 222 | 682 | 0 | 99 |

**3 mismatches in 17,022 checks, 0.018%.** The spec's gate is stricter than
that — "any mismatch is a bug in S2" — so the test reports `pass: false`, and
the three are listed rather than excused. It asks for 20 randconfigs; four were
run, each needing its own `sound/` build.

Running more than one configuration was essential. `allmodconfig` under
`sound/` makes *everything* a module: on its own it never exercises the
`builtin` condition, which is exactly the part kreach adds on top of kmax.
`defconfig` and the randconfigs supply the built-in side.

The test began at **8** mismatches and each one was a real modelling gap:

- **`sound/last.o`** — predicted `m`, not built. It sits inside
  `ifeq ($(CONFIG_SND),y)`, two lines below an ordinary
  `obj-$(CONFIG_SND) += core/ pci/ …`. An exact-y test means the object is not
  compiled *at all* unless the symbol is y — a third kind of Makefile test
  beyond the selector/guard split S2 started with. The symbols cannot be
  collected globally, because the same `CONFIG_SND` is an ordinary selector on
  the line above, so S2 now tracks which objects and directories actually sit
  inside such a block (294 objects, 3 directories).
- **Three KUnit test objects** — predicted `y`, built as `m`. The guard set was
  global, and `CONFIG_SND_SOC_TOPOLOGY_KUNIT_TEST` is an `ifneq (…,)` guard on
  one line and an `obj-$()` selector on the next. Protecting it everywhere lost
  the selector reading. The guard set is now scoped per object.
- **`sound/soc/soc-acpi.o`** — same cause one level down. The part carries no
  CONFIG symbol of its own (`snd-soc-acpi-y := soc-acpi.o`); its selector is on
  the composite. Parts now inherit their composite's selectors.
- **14 composite module targets** — Kbuild only links them when they are
  modules, so at y the file does not exist while its code is in vmlinux. A
  definitional difference, now counted in its own bucket.

What remains, all in one randconfig: two objects predicted `n` that were built
`y` (a kmax formula gap), and one composite missing from the composite set,
which is harvested from the `allmodconfig` build's `.mod` files and so misses
targets that configuration does not build as modules. An S2 error of this kind
costs a wasted witness build and never a wrong finding, because S5 verifies
each witness against real Kconfig and S6 confirms it with a link.

## S4: what the candidate list is made of

12,315 pairs survive, out of which the interesting rejections are:

- **993 composite callers skipped.** A module built from several sources is
  linked into one object whose undefined symbols are the union of its parts'.
  Attributing a reference to that object loses whatever guards the part that
  really made the call: `sound/soc/snd-soc-core.o` is conditional on
  `SND_SOC && SND`, while the part that actually calls `snd_ac97_reset`,
  `sound/soc/soc-ac97.o`, additionally needs `SND_SOC_AC97_BUS`. The first two
  witnesses S6 ever built were both this mistake, and both were correctly
  rejected because the build succeeded. Preferring the parts cut candidates
  from 15,361 to 5,451 at the time it was added.
- **679 pairs dropped as already guarded**, 635 of them because a header
  reaches the symbol through `IS_REACHABLE` and 44 at the call site itself.
  That is the S4 refinement the spec asks for, and it is doing most of the
  work.
- **71 candidates skipped because this architecture never declares the
  provider's option.** `arch/<SRCARCH>/` is in S2's default scope so the
  architecture's own providers have presence conditions at all; without it S4
  matched a same-named object elsewhere in the tree.
- **3,083 references have no provider** anywhere kreach can see, and **543**
  have no presence condition. Both are the subsystem restriction showing
  through, and both cost recall only.

## S5: what the sweep found

The complete sweep: 12,315 candidate pairs collapsed to **7,550 distinct
queries**, solved in **1.6 hours** on one core.

| | |
|---|---|
| unsatisfiable | 7,402 |
| satisfiable, witness accepted by Kconfig | 55 |
| satisfiable, witness rejected before any build | 93 |

The unsat majority is the point rather than a disappointment: most
caller/provider pairs inside a subsystem are already protected by a
`depends on`, and K says so. The interesting population is small by
construction.

The 93 rejections are all one cause, measured in
[docs/design.md](design.md): Kconfig computing a level from a conditional
default that the Boolean encoding treats as free. They run both ways —
roughly half `y`→`m` and half `m`→`y` — which is what tells you it is a value
computation rather than a missing constraint. Each costs a couple of seconds
rather than a build.

So of 148 queries the solver called satisfiable, 55 produced a witness real
Kconfig would honour: **the encoding gap costs about 63% of the satisfiable
population**, and closing it is the single highest-value piece of work left
on this codebase.

## Confirmed findings

All eleven build-confirmed findings on v6.17 x86_64, all `y/m` — the case the
spec names as the main target — in two classes:

| caller | provider | symbol | link | `Fixes:` |
|---|---|---|---|---|
| `CONFIG_SND_SOC_FSL_XCVR` | `BITREVERSE` | `byte_rev_table` | 21 s | `28564486866f` ("ASoC: fsl_xcvr: Add XCVR ASoC CPU DAI driver") |
| `CONFIG_SND_SOC_IDT821034` | `BITREVERSE` | `byte_rev_table` | 118 s | `e51166990e81` ("ASoC: codecs: Add support for the Renesas IDT821034 codec") |
| `CONFIG_SND_SOC_INTEL_KEEMBAY` | `BITREVERSE` | `byte_rev_table` | 24 s | `c5477e966728` ("ASoC: Intel: Add KeemBay platform driver") |
| `CONFIG_SND_SOC_FSL_AUD2HTX` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 252 s | `8a24c834c053` ("ASoC: fsl_aud2htx: Add aud2htx module driver") |
| `CONFIG_SND_SOC_FSL_LPC3XXX` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 544 s | `0959de657a10` ("ASoC: fsl: Add i2s and pcm drivers for LPC32xx CPUs") |
| `CONFIG_SND_SOC_ROCKCHIP_I2S` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 208 s | `4495c89fcf26` ("ASoC: add driver for Rockchip RK3xxx I2S controller") |
| `CONFIG_SND_SOC_ROCKCHIP_I2S_TDM` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 491 s | `081068fd6414` ("ASoC: rockchip: add support for i2s-tdm controller") |
| `CONFIG_SND_SOC_ROCKCHIP_PDM` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 142 s | `fc05a5b22253` ("ASoC: rockchip: add support for pdm controller") |
| `CONFIG_SND_SOC_ROCKCHIP_SAI` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 755 s | `cc78d1eaabad` ("ASoC: rockchip: add Serial Audio Interface (SAI) driver") |
| `CONFIG_SND_SOC_ROCKCHIP_SPDIF` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 54 s | `f874b80e1571` ("ASoC: rockchip: Add rockchip SPDIF transceiver driver") |
| `CONFIG_SND_SUN50I_DMIC` | `REGMAP_MMIO` | `__devm_regmap_init_mmio_clk` | 368 s | `9fc2c8ed923d` ("ASoC: sunxi: Add Allwinner H6 Digital MIC driver") |

One of the link errors is worth a second look:

```
include/linux/bitrev.h:23:(.text+0x1b66ac): undefined reference to `byte_rev_table'
```

The reference is emitted from the inline in the header, not from the driver's
own line. A source-level scan would attribute it to `bitrev.h` and find no
caller option at all, which is the concrete reason the spec insists references
come from binaries: by the time the object exists, the compiler has already
decided where the call lives.

Every `Fixes:` commit is the one that added the driver, which is the finding
restated: these have been missing the dependency since they landed. Each
caller option is its own group, so each is one patch.

### Candidate precision

The spec calls this the interesting number, because S6 makes reported
precision 100% by construction and what is left to measure is how many
candidates S6 rejects — that is, the quality of S4.

| | |
|---|---|
| confirmed | 11 |
| rejected: build succeeded | 2 |
| rejected: build failed another way | 1 |
| settled by a pre-check, no build spent | 3 |
| **precision over builds spent** | **11/14 = 0.79** |

0.79 out of a candidate list that started at 12,315 pairs. Three refinements
compound to get there, and none of them would be enough alone:

- the `IS_REACHABLE` header scan drops 635 pairs before any solving
- composite callers are replaced by their parts, so a reference is attributed
  to the object that actually makes it
- the two pre-flight checks settle witnesses that would have linked cleanly,
  for the price of a `make allnoconfig` or one compile

The `REGMAP_MMIO` class is the one that shows why the build gate matters. It
held twelve candidates. Ten were built: eight confirmed by a link that failed
on `__devm_regmap_init_mmio_clk`, one rejected because the build succeeded,
one rejected because the build failed for an unrelated reason. The other two
never reached a build -- compiling the caller object took twenty seconds each
and showed the symbol was no longer referenced. Reading the Kconfig stanzas
suggested all twelve were real; four of them are not, and only the build
distinguishes them.

Each of the three `BITREVERSE` drivers includes
`<linux/bitrev.h>` and calls `bitrev*`, which on an architecture without
`HAVE_ARCH_BITREVERSE` reads `byte_rev_table` — exported by `lib/bitrev.c`
under `CONFIG_BITREVERSE`. None of their Kconfig stanzas selects it:

```
config SND_SOC_IDT821034            config SND_SOC_INTEL_KEEMBAY
        tristate "..."                      tristate "Keembay Platforms"
        depends on SPI                      depends on ARCH_KEEMBAY || COMPILE_TEST
                                            depends on COMMON_CLK
config SND_SOC_FSL_XCVR                     select SND_DMAENGINE_PCM
        tristate "..."                      select SND_SOC_GENERIC_DMAENGINE_PCM
        select REGMAP_MMIO
        select SND_SOC_GENERIC_DMAENGINE_PCM
        select SND_SOC_FSL_UTILS
```

For contrast, the driver immediately above `SND_SOC_FSL_XCVR` in the same file
does select it, which is what the XCVR stanza is missing:

```
config SND_SOC_FSL_SPDIF
        tristate "Sony/Philips Digital Interface (S/PDIF) module support"
        select BITREVERSE                  <-- present here
        select SND_SOC_FSL_UTILS
```

The first finding in full:

```
id          (stable hash of CONFIG_SND_SOC_FSL_XCVR, CONFIG_BITREVERSE, byte_rev_table)
case        y/m   caller built-in, provider a module: vmlinux link error
caller      sound/soc/fsl/snd-soc-fsl-xcvr.o   [CONFIG_SND_SOC_FSL_XCVR]
provider    lib/bitrev.o                       [CONFIG_BITREVERSE]
symbols     byte_rev_table
levels      CONFIG_SND_SOC_FSL_XCVR=y, CONFIG_BITREVERSE=m
witness     480 symbols enabled

fsl_xcvr.c:(.text+0x192061): undefined reference to `byte_rev_table'
ld: fsl_xcvr.c:(.text+0x192077): undefined reference to `byte_rev_table'
ld: fsl_xcvr.c:(.text+0x19208d): undefined reference to `byte_rev_table'
```

`sound/soc/fsl/fsl_xcvr.c` includes `<linux/bitrev.h>` and calls `bitrev32()`,
which on an architecture without `HAVE_ARCH_BITREVERSE` reads
`byte_rev_table` — exported by `lib/bitrev.c` under `CONFIG_BITREVERSE`. The
sibling driver immediately above it in the same Kconfig file does select it:

```
config SND_SOC_FSL_SPDIF
        tristate "Sony/Philips Digital Interface (S/PDIF) module support"
        select BITREVERSE                  <-- present here
        select SND_SOC_FSL_UTILS

config SND_SOC_FSL_XCVR
        tristate "NXP Audio Transceiver (XCVR) module support"
        select SND_SOC_FSL_UTILS           <-- and nothing pulls in BITREVERSE
```

This is the shape the spec describes: a caller built into vmlinux, a provider
that Kconfig is free to make a module, and no constraint stopping it. The
witness came out of real Kconfig and the link error is the predicted one for
the predicted symbol.

### Feedback on the spec's fix policy

All eleven findings land on a case the spec's fix rules do not cover, because
neither provider has a prompt. `BITREVERSE` and `REGMAP_MMIO` are both bare
library tristates that exist only to be selected:

```
config BITREVERSE
        tristate
        select GENERIC_BITREVERSE if !HAVE_ARCH_BITREVERSE

config REGMAP_MMIO
        tristate
```

So `depends on BITREVERSE` would leave the driver unselectable, and the fix a
maintainer would apply is `select BITREVERSE`, exactly as `SND_SOC_FSL_SPDIF`
already does. That is the one option the spec excludes ("Never suggest
`select` as the fix"), and every one of the eleven records carries the caveat
saying so.

The rule is well motivated — `select` on a symbol that has its own
dependencies is how the unmet-dependency bugs kismet finds get created — but it
does not hold for a dependency-free helper. kreach keeps to the spec, reports
the `depends on` form, and attaches the caveat rather than quietly emitting a
fix that cannot be applied:

```
caveat  BITREVERSE has no prompt, so it is only ever enabled by a select;
        'depends on BITREVERSE' would leave SND_SOC_FSL_XCVR unselectable.
        This one needs a maintainer decision, not the standard fix.
```

## Re-confirmed at v7.3

Findings are only worth sending against a tree a maintainer would apply to, so
the set was re-run two releases on, at **v7.3** — the newest release kmax 4.10
can parse. A separate work directory, S2 scoped to `sound/`, `lib/` and
`drivers/base/` where the providers live, and S5/S6 filtered to the eleven
known caller options: 68 queries in 2.4 minutes instead of 4,971.

| | |
|---|---|
| still confirmed by a link error | **10** |
| no longer reproducible | 1 |

**10 of 10 builds spent were confirmations.** The published set is
[results/v7.3/](../results/v7.3/). One finding drops out of the v6.17 eleven,
and it is worth stating precisely:

`CONFIG_SND_SOC_IDT821034` **no longer reproduces.** Its Kconfig stanza still
lacks the `select`, so a static reading says the bug is live — but the caller
no longer emits the reference:

```
sound/soc/codecs/idt821034.o does not reference byte_rev_table in this
configuration, so the link cannot fail on it
```

Reading the declaration tells you whether anyone fixed the *rule*. Only
compiling the caller tells you whether the *call* is still there. This is the
case that justifies the compile-the-caller pre-check on its own, and it cost
twenty seconds rather than a build.

`CONFIG_SND_SOC_FSL_XCVR` is **unprovable here, not disproven** — and the
reason is more interesting than "the machine is too small", because nine other
witnesses built on the same machine without trouble. Its witness is an
outlier:

| witness | settings enabled |
|---|---|
| `FSL_XCVR` | **3,822** |
| `ROCKCHIP_SPDIF` | 842 |
| `ROCKCHIP_I2S` | 841 |
| `FSL_AUD2HTX` | 700 |
| `INTEL_KEEMBAY` | 696 |
| `FSL_LPC3XXX` | 505 |

4.5x the next largest and 8x the median. The others are small kernels that
build in minutes; that one is a substantial kernel, and its build exhausted
first memory (`ld` OOM-killed) and then disk across four attempts. At v6.17
the same finding had a 480-setting witness and linked in 21 seconds.

The gap was in S5's minimisation, not the hardware, and it has since been
closed. The old core loop released about one symbol per solver call, which is
fine for a few hundred candidates and hopeless for a few thousand: it released
412 in 400 rounds, never reached a satisfying assignment, and discarded the
pass. Removing candidates in blocks instead, iterated to a fixed point, takes
that witness from **3,822 settings to 1,078** in 37 seconds — into the same
range as its peers. See [docs/design.md](design.md) for why blocks and what
bounds them.

S5 also now flags a witness that is far larger than the median, S6 repeats the
warning before building, and S7 carries the count into the record. A witness
nobody can build should say so before the hours go in.

One thing changed upstream that the tool picked up without help: `BITREVERSE`
now selects `GENERIC_BITREVERSE if !HAVE_ARCH_BITREVERSE`, and `bitrev.o` is
gated on the latter, so v7.3 findings name `GENERIC_BITREVERSE` as the
provider. The fix to send is still `select BITREVERSE`, which does the right
thing on architectures that have a native implementation and need no
`bitrev.o` at all.

## What went wrong that the tests caught

Four bugs found by the acceptance tests and unit tests rather than by reading:

- **Level variables were not mutually exclusive.** kclause writes "on iff y xor
  m", which permits *both* level variables true while the symbol is off —
  enough to make S2's `builtin` relabelling unsound. The spec's phrase is
  "never both true"; it needs its own constraint.
- **`.json.gz` files were written uncompressed.** `write_json` goes via a
  `.tmp` name, which does not carry the `.gz` suffix, so the compression
  decision was made on the wrong name.
- **Kbuild's own list variables were read as module names.**
  `obj-$(CONFIG_X) += foo.o` has the same shape as a composite module
  assignment, so `obj` became a module name.
- **Options the architecture does not declare were unconstrained.** Sixteen
  consecutive false candidates in the S5 sweep traced back to
  `CONFIG_GENERIC_HWEIGHT`, which guards `lib/hweight.o` but which x86 never
  declares. With no type from kextract, nothing tied `X==y` to `X`, and
  "built but not built in" became satisfiable for an object that is never
  built. Such symbols are now pinned to n; see `docs/design.md`.
- **An OOM-killed linker was recorded as a rejection.** `make` reports a child
  killed by a signal as `128 + signum`, so the OOM killer taking `ld` arrives
  as `Error 137` — indistinguishable from a build failure unless you know the
  number. It linked for nine witnesses and died on the tenth, which is the
  worst shape this class takes: non-deterministic, looks like an answer, and
  silently drops a finding. Now `build_environment_error` and retried.
- **Two S6 runs on one work directory corrupted each other's builds.** They
  clear each build directory before using it, so one removed the tree the
  other was compiling into; the result was
  `fatal error: generated/autoconf.h: No such file or directory`, recorded as
  a terminal rejection of a perfectly good candidate. The stage now takes an
  exclusive lock on the work directory.
- **A build killed by a signal was recorded as a rejection.** Interrupting an
  S6 run left `rc = -15` records classified as `rejected_other_error` — which
  would drop a real finding and feed a false entry into S4's feedback loop. A
  negative return code now means `build_interrupted`, which is not a terminal
  verdict, so the next run retries it. Only terminal verdicts count as done.
- **Reused build directories broke the next build.** S6 prunes objects after
  each build to bound disk, and rebuilding the same witness into the pruned
  directory produced `fixdep: error opening file: scripts/.sorttable.d` —
  a failure that looks exactly like a compile error and would have been
  recorded as one. Each build now starts from a directory removed and
  recreated.
- **Two distinct queries could share a witness file.** The finding id is a hash
  of caller option, provider option and symbols, as the spec specifies — but
  two queries with those three equal and different presence conditions are
  different questions. The witness and build directory are now keyed by a
  separate query id.

## Working around kmax 4.10

Four things in the installed kmax do not behave as documented. kreach routes
around each rather than patching the package; `docs/design.md` has the detail.

- `Arch.dump_arch()` passes formula *content* where its dump methods want a
  *path*, so it cannot write the files.
- `dump_kclause(dump_composite=True)` pickles the SMT-LIB string while
  `load_kclause(is_composite=True)` reads it as text.
- `Arch.get_selects()` returns `{selected: {selecter: [clauses]}}`, not the
  list of single-entry dicts its docstring describes.
- `Arch.get_dir_dep()` is generated with tristate support disabled — kclause
  says so on stderr — which collapses `X==m` to `X`. Using it for R6 made the
  randconfig result *worse*, because `drivers/scsi/Kconfig` writes this very
  check by hand as `depends on m || SCSI_SAS_ATTRS != m`.

## Evaluation

### Historical-recall benchmark

Mining works: 341 commits since v6.0 mention `undefined reference` or
`IS_REACHABLE`, **209 of those change only Kconfig files**, and of the 60 most
recent, **31 yield an extractable caller/provider pair**. The fix patterns in
those real commits line up with what S7 suggests — `depends on X || !X` is the
most common single pattern, 8 of 31.

Scoring is implemented and tested (`kreach score`), but the protocol requires
checking out each fix's **parent** commit and running the pipeline there, and
S1–S3 are per-commit. `tools/bench_replicate.sh` does one case end to end.
Only **2 of the 31** usable cases have a `sound/` caller option, so a
meaningful recall number for this subsystem needs either those two parents
built, or the caller side widened beyond `sound/` — which is a disk question,
not a missing-code question. That is the main piece of the evaluation left
undone, and it is stated as such rather than estimated.

### Comparison with randconfig

The spec asks which of the tool's findings randconfig also hits, and how many
builds that takes. The second half needs no builds: a randconfig hits a finding
exactly when its `.config` satisfies the finding's own condition — the caller
built in, the provider less available — and that is a check against the
generated file. `kreach randcompare` runs N `make randconfig` invocations,
seconds each, and reports per finding how many of them would have hit it.

**0 of 11 findings were hit by 25 randconfigs**, in 86 seconds. Each
finding's own witness does satisfy its condition, so the check fires when it
should; 25 samples simply never produced a configuration with one of these
drivers built in and its provider a module.

That is the argument for the method stated as a measurement. These are not
configurations that randconfig stumbles into: with 18,333 symbols, the chance
of landing on this particular combination is small, which is why bugs of this
shape survive to a release and then get found one at a time. 25 samples bound
the per-finding rate below roughly 4%; a tighter number needs more samples,
which is cheap — the expensive part, the builds, is what this check avoids.

### Cost

| | |
|---|---|
| S1, per commit and architecture | 1.5 min |
| S2, whole tree + `arch/x86` | 14.1 min |
| S3, `allmodconfig` built under `sound/` | 8.2 min |
| S4 | 6 s |
| S5, 7,550 queries | 1.6 h, about 0.8 s per query |
| S6, 14 `vmlinux` links | 21 s to 755 s, median 212 s; 0.96 h in total |
