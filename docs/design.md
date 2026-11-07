# kreach design notes

This file records the decisions that are not obvious from the code, and the
measurements behind them. Everything here was measured on Linux **v6.17**,
`ARCH=x86_64`, gcc 13 (Ubuntu 24.04), 4 cores.

## The encoding

Three variables per tristate symbol, following kclause's naming so that its
formula and ours compose:

```
CONFIG_FOO        FOO is enabled at some level (y or m)
CONFIG_FOO==y     FOO is built into vmlinux
CONFIG_FOO==m     FOO is a module
```

kclause only emits the `==y` / `==m` variables for symbols Kconfig itself
compares against y or m. kreach needs them for every tristate that can be a
caller or a provider, so S1 adds the linkage for all of them:

```
FOO            <->  (FOO==y) or (FOO==m)
not (FOO==y and FOO==m)                       the spec's "never both true"
(FOO==m)       ->   CONFIG_MODULES
MODULES=n      ->   not (FOO==m)  for every tristate FOO
```

The mutual exclusion is a separate constraint on purpose. kclause writes the
first line as an exclusive-or, which permits *both* level variables being true
while the symbol is off — enough to make the `builtin` relabelling below
unsound. `tests/test_formula.py::test_tristate_levels_are_exclusive` pins it.

### Which formula format S1 writes

The spec allows "DIMACS or SMT-LIB". S1 always writes `s1/model.smt2`, which is
what every later stage reads, because the pipeline talks to z3 directly and
SMT-LIB round-trips through `z3.parse_smt2_string` without a variable map to
keep in step. DIMACS is available with `kreach s1 --dimacs`: it runs z3's
`tseitin-cnf` tactic and writes `s1/model.dimacs` with the CONFIG-name map
beside it, for feeding the model to a SAT-only tool.

That path is tested on a small model
(`tests/test_formula.py::test_dimacs_round_trip_keeps_satisfiability`, which
checks the header counts and that the clause count matches) but **has not been
run on a whole architecture** — CNF conversion of 111,355 constraints is memory
hungry and the container has 3.9 GB. Treat it as the escape hatch it is, not as a
path the pipeline exercises.

## S1 repairs

`kreach test s1` compares K against real `make randconfig` output, as the spec
specifies. On the first run, **0 of 4** randconfigs satisfied K. Each failure
came back as a one- or two-literal unsat core, which made the causes
diagnosable one at a time (`tools/probe_*.py`). Six classes turned up; five are
repaired by default in `kreach/s1_repair.py` and one is deliberately not.

| | problem | repair | fired on v6.17 x86_64 |
|---|---|---|---|
| R1 | kclause emits `X==m -> not rev_dep(X)`: a `select`ed tristate may never be a module. Forbids exactly the configurations kreach looks for. | Replace with the level-aware select semantics built from kclause's per-selecter data: a select active at y forces the target to y, one active at m only forces it on. The select's strength is the *tristate* minimum of the selecter's value and its visibility, which matters when a `bool` selecter sits under a tristate dependency. | 45 constraints replaced by 24,424 over 15,201 select pairs |
| R2 | `imply` carries no constraint, but kextract drops it entirely, so a symbol reachable only through an `imply` looks orphaned and kclause forces it false (`NVMEM_LAYOUTS`). | Replace the flat `not X` with X's own dependency. | 166 symbols |
| R3 | A symbol declared in several Kconfig files keeps only one dependency. `CPU_MITIGATIONS` is declared in `arch/Kconfig` under `if !ARCH_CONFIGURES_CPU_MITIGATIONS` and again as a plain x86 `menuconfig`; x86 selects the guard, so the symbol and everything under it became unreachable. | OR the declaration conditions back into the dependency, when some declaration admits the symbol in a configuration the dependency forbids. | 3 symbols |
| R4 | kextract evaluates `$(success,...)`, `$(cc-option,...)` and friends itself, outside the kernel's make, and gets them wrong: `TOOLS_SUPPORT_RELR` comes out `def_bool 0`, `X86_X32_ABI` gets dependency `0`. | Take the values from this build host, as the spec asks: `make allnoconfig` says what is forced on, and where kextract folded the whole condition into a literal the symbol is left unconstrained. | 131 symbols scanned, 43 handled (28 forced on) |
| R5 | A non-visible selected symbol with no default takes *exactly* its selecters' value, not just at least. Without the upper bound, a model of K sets such symbols to y where `olddefconfig` makes them m. | Bound them above too. **Off by default** (`--r5`); written to `s1/realisable.smt2` regardless, for the witness solver — see "Realisable is stronger than valid" below. | 1,776 bounds computed; as part of K, measured to reject configurations `randconfig` really produces |
| R6 | `depends on` loses the level cap. kclause encodes it as a Boolean implication, so K admits `SND_SOC==y` while `SND==m`. Kconfig caps a symbol at the value of its dependency expression, so with `SND=m` the most `SND_SOC` can be is `m`. | `X==y -> dep(X) relabelled to y`, using the same relabelling S2 uses for object levels. Tristate targets only: Kconfig promotes a *bool* whose visibility is `mod` back to y. The dependency comes from kextract's own `dep` line, not kclause's `get_dir_dep()` — see below. | 8,353 clamps |

R5 is off because the costs are not symmetric. R1–R4 all *relax* K, and a wrong
relaxation costs at most one witness: S5 materialises every witness with the
kernel's own Kconfig (`make KCONFIG_ALLCONFIG=<fragment> allnoconfig`) and
re-checks that the levels the query asked for survived, before S6 spends a
build. R5 *adds* constraints, and a wrong one removes configurations silently —
which costs findings with no signal. Measured, R5 took direction 1 from 4/8 back
to 3/8, so it stays behind a flag. R6 also adds constraints, but it encodes a
Kconfig rule exactly rather than approximating one, and it is what made S5's
witnesses survive Kconfig at all (below).

### Realisable is stronger than valid

K is the set of configurations Kconfig *accepts*. A witness has to clear a
higher bar: it has to be a configuration we can write to a file and hand to the
kernel's Kconfig. Two things get in the way, and both showed up as S5 rejecting
its own witnesses before S6 spent a build on them.

**Non-visible symbols.** 3,016 of v6.17 x86_64's 18,333 symbols have no prompt,
so a `.config` cannot set them; Kconfig recomputes them from what selects them.
`sound/soc/codecs` is almost all like this. The first S5 run produced ten
witnesses and Kconfig threw away the value in all ten
(`CONFIG_SND_SOC_AD1980: y -> n`). S1 therefore writes the R5 upper bounds, plus
a flat "off" for non-visible symbols that nothing selects and that have no
default, to `s1/realisable.smt2` — 2,161 constraints that the witness solver
adds and that K itself does not carry.

**The missing level cap.** With realisability in place, the remaining
rejections were `CONFIG_SND_SOC: y -> m`: K had allowed the caller to be
built in while its own dependency was a module. That is R6.

Witness rejection does not go to zero and should not be read as a failure
mode: it is the designed place for an S1 encoding gap to surface. A rejected
witness costs one `make allnoconfig` -- a second or two -- instead of a
`vmlinux` link, and every rejection is written to `s5/queries.jsonl` with the
symbol and the two levels, which is how the direction-1 failures above were
found in the first place.

### The one gap that is not worth closing here

14 rejections in the first 3,050 queries of the sweep all reduce to one cause,
and it is worth stating exactly because it is also what is left of the
direction-2 drift. The rejections look like a dozen Wolfson codecs:

```
CONFIG_SND_SOC_WM8510   wanted y, kconfig gave m
```

Each depends on `SND_SOC_I2C_AND_SPI`, which is a non-visible tristate whose
value comes entirely from conditional defaults:

```
config SND_SOC_I2C_AND_SPI
        tristate
        default m if I2C=m
        default y if I2C=y
        default y if I2C=n
```

R6 correctly requires `SND_SOC_I2C_AND_SPI==y` for the codec to be y, and the
solver obliges. Kconfig then computes the symbol as `m`, because `I2C=m` in
the witness and the first matching default wins. The level is not a choice the
configuration can make -- it is a function of `I2C`.

kextract does not record it. All three defaults come back with the same value
field:

```
def_bool CONFIG_SND_SOC_I2C_AND_SPI 1|(… and CONFIG_I2C==m)
def_bool CONFIG_SND_SOC_I2C_AND_SPI 1|(… and CONFIG_I2C==y)
def_bool CONFIG_SND_SOC_I2C_AND_SPI 1|(… and CONFIG_SPI_MASTER)
```

`default m if …` and `default y if …` are both flattened to `1`. Modelling
this means reading the Kconfig sources for the default *values* and
implementing Kconfig's first-match-wins evaluation over them -- that is a
tristate-valued expression evaluator for Kconfig, which is the thing the
spec's non-goals rule out, and it is where this stops.

The cost is bounded and visible, and now measured. Over the complete 7,550-query
`sound/` sweep:

| | |
|---|---|
| unsatisfiable | 7,402 |
| satisfiable, witness accepted by Kconfig | 55 |
| satisfiable, witness rejected | **93** |

The 93 span 45 distinct symbols, not just the Wolfson codecs above -- the most
frequent are `SND_SOC_RL6231`, `SND_SEQ_DEVICE` and `SND_SOC_WM_ADSP`, all
non-visible tristates whose level is computed rather than chosen. So roughly
**five of every eight satisfiable queries produce a witness Kconfig will not
honour**, and the rejections run both ways — 51 where the solver wanted `y`
and Kconfig computed `m`, 41 the other way, one `m` to `n`. Both directions is
the signature: this is not a missing constraint in one direction, it is Kconfig
deciding a level that the Boolean encoding leaves free.

Each rejection costs one `make allnoconfig`, a second or two, instead of a
`vmlinux` link — so it is recall, not precision, and cheap recall at that.
But 93 is a large enough number to make tristate-valued default evaluation
the clear next piece of work on this codebase: it would convert most of those
into either a real finding or an honest `unsat`, and it is the same work that
would close the last of the S1 acceptance gap and the `CONFIG_INPUT` false
positives in S4.

With R5-as-realisability and R6 together, the first 40 witnesses after the fix
were all kept, up from **10 of 10 rejected** before it. That cured the
non-visible-symbol and missing-level-cap rejections; the 93 above are the
conditional-default cause, which is a different gap and still open.

### Where the dependency expression comes from matters

R6's first implementation took the dependency from kclause's `get_dir_dep()`
and made the randconfig result *worse* — 4/8 down to 1/10, with three of the
eight seeds failing on one core:

```
CONFIG_SCSI_ENCLOSURE==y, CONFIG_SCSI_SAS_ATTRS==y, Not(CONFIG_MODULES)
```

`tools/probe_r6.py` checks each randconfig against K and against the R6 clamps
alone, which placed the blame on R6 directly rather than on an interaction.
The cause is that `get_dir_dep()` is produced with tristate support disabled —
kclause says so on stderr — so it collapses `X==m` to `X`. And the symbol in
question is the kernel writing this very check by hand:

```
config SCSI_ENCLOSURE
        tristate "SCSI Enclosure Support"
        depends on SCSI && ENCLOSURE_SERVICES
        depends on m || SCSI_SAS_ATTRS != m
```

Collapsed, `SCSI_SAS_ATTRS != m` becomes "SCSI_SAS_ATTRS must be off", so K
rejected every configuration with both symbols built in — which with
`MODULES=n` is a configuration Kconfig is perfectly happy with. kextract's own
`dep` line keeps the `==m`, so R6 reads the dependency from there instead.
With that, all three seeds pass and the R6 clamps reject none of the eight
randconfigs. R2 and R3 take their dependency from the same place for the same
reason.

### S1 acceptance, measured

| | first run | with R1–R4 | R1–R4 + R6 |
|---|---|---|---|
| randconfigs satisfying K | 0/4 | 4/8 | **4/8** |
| R6 clamps alone rejecting a randconfig | — | — | **0/8** |
| models of K surviving `make olddefconfig` unchanged | 0/4 | 0/8 | 0/10 |
| drift per model (of 18,333 symbols) | 73–1,394 | 73–1,394 | 63–182 (median 123) |
| S5 witnesses Kconfig kept | — | 0/10 | **55/148** over the full sweep |

The spec asks for 1,000 samples in each direction and these are 8 and 10, so
they say which way the repairs moved the number, not what the number is. Each
sample costs a `make randconfig` plus a solver call on a 111,355-constraint
formula; `kreach test s1 --samples 1000` is the run that would settle it.

The sample sizes differ by row because the directions were re-measured at
different points: direction 1 and the R6-clamp check over 8 randconfigs after
the `dep`-line fix, direction 2 over the 10 recorded in
`acceptance-s1.json`, whose R6 still took the dependency from
`get_dir_dep()`. Direction 2 was not re-run after that fix, so its drift
figures are a lower bound on the improvement, not an upper one.

The four remaining direction-1 failures are all one family: Kconfig's
*value* propagation through `select` where the symbols involved are also
constrained some other way (`HWMON==m` against a bool selecter,
`DRM_SIMPLEDRM` against `FB_SIMPLE`). They have the same root as the witness
rejections described below — Kconfig deciding a level that the Boolean
encoding treats as free — and the same answer: it needs a tristate-valued
evaluator for Kconfig expressions rather than a Boolean encoding with level
variables bolted on, which is squarely in the "not a full variability-aware
checker" non-goal.

Direction 2 (`olddefconfig` leaves a model of K unchanged) is the strong half of
the test and is **not** met. R6 improved it by roughly an order of magnitude —
a model of K now drifts by a median of 123 symbols out of 18,333, down from
several hundred — because most of the drift was `y`↔`m` on symbols whose
dependency is a module. What is left is `y`↔`m` on non-visible selected symbols
and `y`↔`n` on `ARCH_HAS_*`-style symbols: Kconfig's default and
reverse-dependency *values* for symbols the user cannot set. R5 is the start of
a fix and is not yet sound.

This costs kreach less than the number suggests, because witnesses are not
written out from a model — they are produced by real Kconfig from a fragment
and then checked, so drift shows up as a rejected witness rather than as a
wrong finding. Over the full sweep that rejection rate is 93 of 148
satisfiable queries, for the one cause set out in "The one gap that is not
worth closing here" above — and a rejection costs a `make allnoconfig`, not a
link.

## Options the architecture does not declare

The S5 sweep threw up sixteen candidates in a row with the same provider,
`CONFIG_GENERIC_HWEIGHT` supplying `__sw_hweight32`, and all sixteen were
false. Two separate mistakes combined to produce them.

**The variables were unconstrained.** `lib/Makefile` has
`obj-$(CONFIG_GENERIC_HWEIGHT) += hweight.o`, but x86 never declares that
option — it has `arch/x86/lib/hweight.S` instead. kextract therefore reports no
type for it, `completion_constraints` skips a symbol with no type, and nothing
ties `CONFIG_GENERIC_HWEIGHT==y` to `CONFIG_GENERIC_HWEIGHT`. With the base
variable free to be true while the `==y` variable is false,
`available(o) and not builtin(o)` is satisfiable for an object that is never
built at all — and that formula is precisely "the provider is a module".

This is a soundness hole rather than an approximation, so it is closed rather
than noted: S2 records every CONFIG symbol Kbuild refers to that the
architecture's Kconfig does not declare, and `formula.undefined_symbols_off`
pins all three of its variables false. klocalizer does the same thing for the
same reason.

**The real provider was out of scope.** S4 picked `lib/hweight.o` because
`arch/` was not in S2's default directory list, so `arch/x86/lib/hweight.o` —
which is what actually defines the symbol on this architecture — had no
presence condition and could not be considered. The default list now includes
`arch/<SRCARCH>/`, and S4 additionally skips any candidate whose provider
option this architecture does not declare, with its own counter in the stats so
the number is visible rather than silently absorbed.

The lesson generalises: a symbol absent from `kconfig_types` is not a symbol
without constraints, it is a symbol pinned to n, and the encoding has to say
so.

## S2: where the y/m split comes from

kmax gives one formula per object: the condition under which the object is
*compiled*. It cannot say at which level, because `obj-$(CONFIG_X) += foo.o`
puts `foo.o` somewhere whenever X is y or m. kreach derives the level split by
relabelling:

```
available(o) = kmax's formula, unchanged
builtin(o)   = the same formula with each selector variable X replaced by X==y
```

Selector versus guard is the subtlety. `obj-$(CONFIG_X)` and
`ifeq ($(CONFIG_X),y)` are exact-y tests, so relabelling them is right.
`ifdef CONFIG_X` and `ifneq ($(CONFIG_X),)` are true for `m` as well, so those
symbols keep their enabled reading; S2 collects that set by scanning the
Makefiles — 144 such symbols in v6.17 across S2's default scope, 19 of them
under `sound/` alone. (An earlier default scope of eight directories found
125; the figure moved when `arch/<SRCARCH>/` was added, for the reason in
"Options the architecture does not declare" below.)
`ifeq ($(CONFIG_X),m)` cannot be expressed by the relabelling at all; 6
symbols use it, and those plus 3 other constructs are recorded in
`s2/unmodelled.json` rather than silently mismodelled.

The relabelling is an approximation for non-monotone Makefile logic, which is
exactly what `kreach test s2` measures against real builds.

### S2 acceptance, measured

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

**3 mismatches in 17,022 checks (0.018%).** Both directions matter and only
together: `allmodconfig` under `sound/` makes *everything* a module, so on its
own it never exercises the `builtin` condition at all — the one kreach adds.
`defconfig` and the randconfigs supply the y side. `--reuse-s3-build` checks
against the build S3 already made rather than making a second one.

The test started at **8** mismatches and every one it found was a real
modelling gap:

- `sound/last.o`, predicted `m`, not built. The exact-y block, described
  above; this is the finding that made the third Makefile reading necessary.
- Three KUnit test objects (`soc-topology-test.o` and friends), predicted `y`,
  built as `m`. The global guard set: `CONFIG_SND_SOC_TOPOLOGY_KUNIT_TEST` is
  an `ifneq (…,)` guard on one line and an `obj-$()` selector on the next, so
  protecting it globally lost the selector reading. Fixed by scoping the
  guard set per object.
- `sound/soc/soc-acpi.o`, predicted `y`, built as `m`. Same cause one level
  down: the part carries no CONFIG symbol of its own
  (`snd-soc-acpi-y := soc-acpi.o`) and inherits its selector from the
  composite. Fixed by propagating composite selectors to parts.
- 14 composite module targets, "predicted y, not built". Kbuild links
  `snd-pcm.o` out of its nine parts only when the module is a module; at y the
  parts go into `built-in.a` and the composite file is never created. kreach's
  `builtin` means "this code is linked into vmlinux", which is the right
  reading for reachability and true of the composite, while the test compares
  against files on disk, where it is false. The two disagree by definition, so
  the test counts them separately rather than calling them bugs — and S4 works
  from the parts anyway.

The three that remain, all in one randconfig:

| object | predicted | actual | reading |
|---|---|---|---|
| `sound/i2c/other/ak4xxx-adda.o` | n | y | kmax formula gap |
| `sound/pci/ice1712/ak4xxx.o` | n | y | kmax formula gap |
| `sound/pci/oxygen/snd-se6x.o` | y | n | composite, but absent from the composite set, which is harvested from the `allmodconfig` build's `.mod` files and so misses targets that configuration does not build as modules |

They are left as measured rather than special-cased. An S2 error of this kind
costs a wasted witness build, never a wrong finding, because S5 verifies every
witness against real Kconfig and S6 confirms it with a link.

## S4: the three cases and why only y/m fires in v1

The default RC rule models the common header pattern — a real declaration under
`IS_ENABLED(CONFIG_BAR)`, a `static inline` stub otherwise:

```
RC(c, s) = available(c) and available(p)
```

Combined with the core property this makes

| case | query | satisfiable under the default rule? |
|---|---|---|
| y/m | `builtin(c) ∧ ¬builtin(p) ∧ available(p)` | **yes** — the main target |
| y/n | `builtin(c) ∧ ¬available(p)` | no |
| m/n | `available(c) ∧ ¬builtin(c) ∧ ¬available(p)` | no |

That is the point rather than a defect: the stub protects the disabled case and
not the module case. y/n and m/n need evidence that the reference survives the
provider being off (`rc_rule = "no_stub_evidence"`), which v1 only has when a
build compiled the caller with the provider disabled. Wide configurations never
do that, so those two cases stay empty until S3 harvests from narrower builds —
the first row of the limitations table below.
`tests/test_formula.py::test_yn_and_mn_are_unsat_under_the_default_rc_rule`
pins both halves of this.

### The guards S4 cannot see, and the loop that catches them

S4's refinement looks for `IS_REACHABLE()` and `IS_BUILTIN()` near the call
site and in the headers that declare the symbol, and on `sound/` that drops
635 pairs. What it cannot see is the same guard written in Kconfig instead of
C. ALSA's jack support is the canonical case:

```
config SND_JACK_INPUT_DEV
        bool
        depends on SND_JACK
        default y if INPUT=y || INPUT=SND
```

`default y if INPUT=y || INPUT=SND` is `IS_REACHABLE` by hand: the symbol that
gates the `input_*` calls is only set when `INPUT` is at least as available as
`SND`. Several `CONFIG_*` → `CONFIG_INPUT` candidates come out satisfiable
because of it, and S6 rejects each of them with a successful build.

Modelling the guard needs the *value* of that conditional default, which is
the one thing kextract does not record (see the S1 notes). So in v1 these are
handled by the mechanism the spec puts there for exactly this purpose: S6's
rejections feed back into S4 through `kreach s4 --use-feedback`, and the pair
is not offered again. What feeds back is only a verdict S6 actually reached —
a build that was interrupted or fell over before linking leaves the pair
unresolved rather than suppressing it for good.

This also sets the shape of the real fix, for whoever does it next: it is the
same tristate-valued default evaluation that would close the last of the S1
acceptance gap. One piece of work buys precision here and recall there.

## S6: making the build budget affordable

The spec's budget is one build per finding, parallel across machines, with
witness minimisation keeping each build short. On one four-core container
neither parallelism nor minimisation alone is enough: the first three
witnesses took 152 s, 442 s and 326 s to link.

Two things fix that, and they interact.

**Objects are pruned after each build.** A `vmlinux` tree is a gigabyte or so,
and findings arrive in the dozens, so S6 keeps the `.config` and the log and
deletes the rest. `--keep-build` turns that off.

**`--ccache` is what makes the pruning free.** Witnesses are minimal
configurations that differ in a handful of options, so successive builds
compile mostly the same translation units — and with the objects gone, a
shared compiler cache is the only place that reuse can live. Rebuilding the
same three witnesses with `--ccache` took **21 s, 118 s and 23 s** instead of
152 s, 442 s and 326 s.

S6 results also accumulate across runs, keyed by query id, so a bounded run
now and the rest later do not throw away what was already built. `--rebuild`
ignores what is on disk.

### What counts as a verdict

Six separate bugs in S6 turned out to be the same mistake: recording
something that is not a verdict as one. Each would have dropped a real
finding silently, and three of them also fed a false entry into S4's
feedback loop, which suppresses the pair on every later run.

| what happened | was recorded as | is now |
|---|---|---|
| build killed by a signal (`rc < 0`) | `rejected_other_error` | `build_interrupted`, retried |
| pruned build directory reused, `generated/autoconf.h` missing | `rejected_other_error` | `build_environment_error`, retried |
| two S6 runs clearing each other's trees | `rejected_other_error` | refused by a work-directory lock |
| witness whose guard Kconfig turned off | `rejected_build_ok` after a full link | `skipped_witness_defanged` in a second |
| caller that no longer emits the reference | `rejected_build_ok` after a full link | `skipped_reference_not_emitted` after one compile |
| OOM killer took `ld` during the link | `rejected_other_error` | `build_environment_error`, retried |

The distinction the code now keeps is between *terminal* verdicts, which
settle a witness and may feed back to S4, and everything else, which leaves
the question open for the next run. `TERMINAL` in `kreach/s6_confirm.py` and
`S6_REJECTIONS` in `kreach/s4_refcond.py` are the two lists, and candidate
precision is reported over builds actually spent rather than over records.

The last of those is the instructive one. `make` reports a child killed by a
signal as `128 + signum`, so an OOM kill arrives as:

```
make[3]: *** [scripts/Makefile.vmlinux_o:79: vmlinux.o] Error 137
```

which is indistinguishable from an ordinary build failure unless you know to
look for the number. `dmesg` had the real story —
`Out of memory: Killed process (ld) ... anon-rss:2485076kB` — `ld` wanting
2.5 GB against 2.7 GB available. Marginal enough that nine witnesses linked
and the tenth did not, which is the worst shape a bug of this class can take:
non-deterministic, looks like an answer, and silently costs a finding.

The general shape is worth keeping in mind for anything built on this: a
pipeline whose precision rests on "the build said so" has to be careful about
what it lets the build say. Every one of these six was found by running the
thing, not by reading it.

### Why the shrinking is done in blocks

The obvious way to minimise is to assume every candidate off and release
whatever the unsat core names, repeatedly. That was the first implementation,
and it works well up to a few hundred candidates. Past that it falls apart,
because the cores come back tiny — roughly one symbol released per solver
call. On a 3,818-candidate witness it released 412 in 400 rounds, never
reached a satisfying assignment, and the pass was discarded entirely, leaving
the unminimised model.

Blocks fix it. Assume a whole block off: if that is satisfiable the entire
block goes in one call; if not, split it and try the halves; a block that
still fails at size one is a symbol the constraints require. Large incidental
regions disappear early, which is the shape of the problem — most of a
bloated witness is accidental rather than forced. Measured on the witness
above, iterated to a fixed point:

```
3818 -> 1964 -> 1228 -> 1201 -> 1080      37 seconds, 4 passes
```

A 72% reduction, and the difference between a witness that cannot be built on
a 4-core, 3.9 GB machine and one that can. Partial progress is kept when the
budget runs out, so a pathological witness shrinks as far as the budget
allows instead of not at all.

Two numbers bound it: `max_passes` (4) and `max_checks` per pass (400). The
pass reports which way it ended — `satisfied`, `hit the check budget`,
`solver gave up` — because "it would not shrink further" and "it ran out of
budget" call for different responses, and a statistic that conflates them
sends you looking in the wrong place. The first version of this code
accumulated its counters after the early `break`, so a pass that hit its limit
reported as having done no work at all; that reading cost an hour of believing
the constraints genuinely forced 3,800 settings.

### Reporting witness size

Minimisation is not a guarantee of smallness, so the size travels with the
finding. S5 takes the median across satisfiable findings and flags anything
past three times it, with a floor so that a small set with one slightly larger
member is not flagged. S6 repeats the warning before it starts building, and
S7 puts the count in the record and the report. The point is that a witness
nobody can build should say so before the hours go in, not after.

### What the link step needs

The `vmlinux` link is the memory high-water mark of a witness build, and it is
single-threaded, so parallelism does not help it. On 3.9 GB of RAM `ld` fits
for most witnesses and not for all; a larger configuration needs more than the
machine has, and no amount of `-j` tuning changes that. If a witness keeps
coming back `build_environment_error`, the machine is the limit rather than the
finding, and it needs either more memory or a build host that has it.

### One S6 per work directory

S6 clears each build directory before using it, so two S6 runs over the same
work directory delete each other's trees. That surfaces as

```
include/linux/kconfig.h:5:10: fatal error: generated/autoconf.h: No such file or directory
```

which looks exactly like a compile error and gets recorded as
`rejected_other_error` -- a terminal verdict on a finding, produced by two
processes colliding rather than by anything about the code. It happened here
when a replacement run was started before the previous one had actually died.

The spec's "parallel across machines" means separate work directories. Within
one, the stage takes an exclusive `flock` on `<work>/.s6.lock` and a second
run refuses to start with a message saying so. Other stages are not blocked,
and separate work directories are not blocked.

### Checking the witness before spending the build

A `.config` can satisfy the caller and provider levels the query asked for and
still exercise nothing. The caller's presence condition names more symbols
than its own option:

```
snd-soc-sdca-$(CONFIG_SND_SOC_SDCA_HID) += sdca_hid.o
```

`sdca_hid.o` calls `hid_add_device`, and S5 produced a witness with
`CONFIG_SND_SOC_SDCA=y` and `CONFIG_HID=m` — both levels exactly as asked.
Kconfig also left `CONFIG_SND_SOC_SDCA_HID` off, so the object was never
compiled, the reference was never emitted, and the build succeeded. S6 drew
the right conclusion — `rejected_build_ok` — after **602 seconds** of linking.

Both stages now re-evaluate the whole query against the concrete
configuration rather than two named symbols: S5 in `check_query_holds` before
writing the witness out, and S6 in `witness_still_exercises` before starting a
build. The check takes about a second. On the next S6 run it skipped such a
witness immediately, with the reason recorded as
`skipped_witness_defanged` so the count is visible, and candidate precision
is reported over builds actually spent rather than over records.

## Does the suggested fix work?

S6 confirms a finding with a build that fails. That is not the same as
showing the patch works, and it is the first thing a maintainer asks.
Nothing in the pipeline answered it, because the pipeline's job ends at
demonstrating the bug.

[../tools/verify_fix.py](../tools/verify_fix.py) answers it separately: for
each finding it adds `select <PROVIDER>` to the caller's stanza, hands the
witness back to Kconfig and links `vmlinux` again. All ten v7.3 witnesses
link once the fix is applied, and with all ten applied at once the four stock
configurations stay Kconfig-clean — which is the check that matters for a
`select`, since `select` on a symbol with its own dependencies is how
unmet-dependency bugs get made.

It is deliberately not part of S7. S7 suggests a fix under the
specification's rules; whether a human's actual patch works is a different
question, asked with a different tool, and keeping them apart stops the
checker from grading its own homework.

## S7: what to do when the standard fix cannot work

The spec names three fixes and rules out a fourth: `depends on BAR`,
`depends on BAR || !BAR`, `IS_REACHABLE()` as a last resort, and never
`select`. S7 picks between the first two by looking for the stub pattern — a
`static inline` under `IS_ENABLED(CONFIG_BAR)` in a header that declares the
symbol — because that is what makes `|| !BAR` right rather than a plain
dependency.

The first confirmed finding showed where that rule runs out. `CONFIG_BITREVERSE`
has no prompt; it is a library tristate that exists only to be selected. A
plain `depends on BITREVERSE` would leave the caller unselectable, because
nothing would ever turn the provider on, and the fix a maintainer would
actually apply is `select BITREVERSE` — the one option the spec excludes.

S7 keeps to the spec and reports the `depends on` form, but attaches a caveat
naming the problem rather than leaving the maintainer to find it. The
visibility comes from S1's `vars.json`, which already records which symbols
have a prompt. A finding whose provider is visible gets no caveat.

## Publishing a result set

`s7/findings.txt` names files the way a work directory does --
`s5/witness/<id>.config`, `s6/build/<id>/build.log` -- which is correct there
and meaningless anywhere else. Published on its own, the report cited evidence
the reader had no way to open.

`kreach s7 --publish DIR` writes the set so that it stands up by itself: the
report, the records, `table.md` and `summary.json`, plus each finding's
witness and link log copied in, with the two path fields rewritten to
`witness/<id>.config` and `build/<id>.log`. The result is checkable without a
work directory, which is the only form worth committing.

One trap, found by falling into it twice. `write_meta` rebuilt the kernel
identity from the current checkout every time any stage recorded a statistic,
and `run_s7` records one at the end -- so publishing a v6.17 set while the tree
sat at v7.3 relabelled the run, silently, because every other field still
looked right. The identity now belongs to S1, which is the stage that decides
which tree a work directory is about; later stages leave
`kernel_commit`/`kernel_describe`/`kernel_version` alone, and re-running S1 is
how you move a work directory to another revision. Published records still
deserve a glance at `kernel` afterwards, since a work directory whose S1 ran
somewhere else is indistinguishable from one that did not.

## Providers from source as well as binaries

The spec is firm that references come from binaries, because the compiler has
already resolved macros, inlining and dead branches. kreach does that.

The provider side is different. A build restricted to one subsystem compiles the
callers but not every provider, so S3 completes the provider map with a
whole-tree scan for `EXPORT_SYMBOL*`. That macro takes the symbol name as a
literal token at file scope, so the scan reliably *locates* a provider; it says
nothing about configuration, which is what S2's presence conditions are for.
Where a build does exist, ELF and `Module.symvers` data takes precedence and the
exact object→module mapping comes from the build's `.mod` files.

## Related work

Several tools check Kconfig, or Kconfig against code. None checks the property
in "The encoding" above, and the reasons differ enough to be worth stating.

**Nadi et al.** is the closest, and the comparison a reviewer will ask for
first. *Mining Configuration Constraints: Static Analyses and Empirical
Results* (Nadi, Berger, Kästner, Czarnecki, ICSE 2014) extracts configuration
constraints from C code using TypeChef, among them linker def/use constraints
built from per-file symbol tables under presence conditions -- the same raw
material S3 and S4 work from. It studied the x86 architecture of Linux
v2.6.33.3 (7,691 C files, 6,559 features) and three smaller systems. Two
figures frame the difference: the two extraction heuristics are **93% and 77%
accurate**, meaning most constraints the analysis emits already hold in the
variability model, and the analysis **recovers 19% of the existing model's
constraints** -- revised to 28% in the journal version, *Where Do
Configuration Constraints Stem From?* (TSE 2015).

Those are the measurements of a different goal. The question there is how much
of a model that already exists can be rebuilt from code, scored by how much
comes back. The question here is which configurations the model fails to
forbid, scored by links that fail. The paper also treats features as Boolean,
so the y/m split this document is mostly about does not arise in it.

**kismet** (kmax suite) finds unmet dependencies caused by `select`: a symbol
forced on while its own `depends on` is unsatisfied. It runs in Intel's 0-day
CI, which is where the `kismet: WARNING: unmet direct dependencies detected`
reports on the kernel list come from. It reads Kconfig only, never code or the
linker, so a caller referencing a symbol that no configuration provides is
outside what it can see at all.

**kconfig-sym-check** is in the tree: `scripts/kconfig/kconfig-sym-check.pl`,
by Andrew Jones, authored 2026-05-27 and first released in **v7.2**. `make
kconfig-sym-check` reports symbols referenced in Kconfig expressions that no
`config` or `menuconfig` entry ever defines. That is a symbol Kconfig does not
know, not a symbol no configuration provides.

**kconfirm** is at RFC (v3, in Rust, not in v7.3). It reports dead code,
constant conditions, invalid ranges and duplicate `depends on`, and its RFC
posting cites 489 alarms on v7.1-rc2 under the default checks. Kconfig-internal
as well, and worth watching: its findings are landing ahead of the tool, as in
`8cdae75cebe0` ("arch/Kconfig: fix dead conditions by removing dead options",
v7.3), whose message credits kconfirm by name.

**randconfig and the 0-day bot** do find bugs of this class, but only when a
random configuration happens to hit one. That is measured rather than asserted
in [results.md](results.md): 0 of 11 findings were hit by 25 randconfigs.

What none of them does is cross the code/Kconfig boundary with tristate levels
attached, which is the gap kreach is aimed at, and the reason every finding
here carries a build.

## Reuse: what was taken from kmax and what was not

The spec says "reuse first" for three stages, so each was evaluated before any
code was written.

**S1 — kclause: used.** It does the Kconfig parsing and encodes `depends on`,
`select`, visibility and choice groups, which is the bulk of the work. kreach
keeps it and adds the level variables and the six repairs above. Writing a new
encoder was never close to worthwhile.

**S2 — kmax: used.** It finds the objects and their presence conditions across
directory descent and composite targets, which is exactly the hard part of
reading Kbuild. What it does not do is distinguish levels: its condition says
an object is *compiled*. The relabelling that splits that into `builtin` and
`available` is the part kreach adds, and it is small.

**S5 — klocalizer: evaluated, not used.** klocalizer already turns constraints
into a buildable `.config`, which is the right shape. The blocker is that
`Klocalizer.get_config_from_model(model, arch, set_tristate_m, ...)` takes
`set_tristate_m` as a single flag for the whole configuration: every tristate
that is on becomes `m`, or none does. kreach's entire question is one symbol at
`y` while another is at `m` in the same configuration, so a global flag cannot
express a witness. Its compilation-unit constraints have the same shape
problem — `include_compilation_unit(unit)` means "this object is compiled", with
no way to ask for it at a particular level.

What kreach does instead is shorter than bending klocalizer around this: read
the levels straight out of the z3 model, write them as a Kconfig fragment, and
let the kernel's own Kconfig turn it into a full `.config` with
`make KCONFIG_ALLCONFIG=<fragment> allnoconfig`. That also gets the
minimisation the spec asks for for free — `allnoconfig` turns off everything the
fragment does not mention — and the file handed to S6 is one an unmodified
`make` produced, which is what the spec requires of the reproducer.

klocalizer's `--repair`/approximate mode remains the thing to reach for if
witnesses ever need to resemble a given starting configuration rather than be
minimal.

## Working notes on the kmax 4.10 API

Three things in kmax 4.10 do not work as documented, and kreach routes around
each rather than patching the installed package:

- `Arch.dump_arch()` passes the formula *content* to its dump methods where they
  expect a *path*, so it cannot write the files. S1 loads and dumps kextract and
  kclause by explicit path instead.
- `dump_kclause(dump_composite=True)` pickles the SMT-LIB string, but
  `load_kclause(is_composite=True)` reads it back as text. S1 writes the
  composite formula as plain SMT-LIB itself, which is also readable on its own.
- `Arch.get_selects()` returns `{selected: {selecter: [clauses]}}`, not the list
  of single-entry dicts its docstring describes.

## How current a kernel can be analysed

kmax bundles its own Kconfig parser, one module per era of the language, and
picks the newest that matches the kernel's version. kmax 4.10's newest is
`v7.1`. That parses every release up to and including **v7.3**, but not
current master: the Kconfig language has since gained a `def_string`
statement (with `def_int` and `def_hex`, in "kconfig: Add def_string, def_int
and def_hex"), and the parser stops on it:

```
scripts/Kconfig.toolchain:219: unknown statement "def_string"
arch/x86/Kconfig:2364: unknown statement "def_string"
```

kextract then exits 0 having written nothing, so the failure arrives as
`kextract file couldn't be generated` rather than as the syntax error that
caused it — worth knowing, because the message sends you looking in the wrong
place.

So the practical ceiling is the newest tagged release, which is close enough
for patch work and will move as kmax is updated. Teaching the bundled parser
the three new statements is a small change, but it belongs upstream in kmax
rather than here.

It follows that kreach is not versioned against the kernel, and should not be.
The ceiling is kmax's, the semantics kreach encodes are Kconfig's and Kbuild's,
and neither moves on the release cadence. What a kernel release produces is a
new *result set* -- its own findings, witnesses and logs, with its own
`kernel_commit` -- which is why the published sets are per revision and the
package version is not. The one case that would change this is the spec's
closing proposal, a lightweight checker in the kernel's own `scripts/`:
anything in-tree is versioned with the kernel by construction, which is an
argument for keeping that candidate small and semantics-only. The README's
Compatibility table records what this version has actually been run against.

## Known limitations

Every limitation costs recall, not precision: S6 confirms each finding with a
real build, so a reported finding is one that failed to link.

| limitation | effect | state |
|---|---|---|
| references come only from wide configs (S3) | code compiled only when some option is *off* is never seen; keeps the y/n and m/n cases empty | open — needs allnoconfig-based and per-arch builds |
| RC is approximated (S4) | candidates S6 rejects waste builds; guarded calls mis-detected | S6 rejections feed back via `kreach s4 --use-feedback` |
| Kconfig encoding gaps (S1) | wrong witnesses, or missed configurations | measured above; witnesses are verified against real Kconfig before any build |
| Makefile conditions too complex to model (S2) | object levels mispredicted | `kreach test s2` measures it; unmodelled constructs listed in `s2/unmodelled.json` |
| toolchain-dependent symbols fixed to the build host | bugs needing another compiler are missed | R4 pins them to this host; run per toolchain |
| architecture-specific Kconfig | each ARCH is a separate run | x86_64 and arm64 both exercised at v7.3. Six of the fourteen arm64 findings the x86_64 sweep did not confirm — not for want of reach, since it raised all six, but because it gets to the `ARCH_MESON` drivers only via `COMPILE_TEST`, whose witnesses carry `OF=n` and leave the calls uncompiled. A per-ARCH run buys witnesses that actually trigger, not just code that is otherwise invisible |
| toolchain probing against the build host (R4) | cross-compiling makes more symbols look unavailable, and they are pinned off | 100 unavailable on an arm64 cross build against 14 native on x86_64; costs recall, not precision |
| weak symbols and linker-script symbols | treated as always provided | excluded from candidates by S4, listed separately in `s3/symbols.json.gz` |
| symbols guarded by `#ifdef` *inside* a present object (S2) | the provider reads as built in while the symbol is compiled out, so that pair is never a candidate | found in `SND_SOC_FSL_AUD2HTX`'s witness: `devm_snd_dmaengine_pcm_register` is inside `#ifdef CONFIG_SND_SOC_GENERIC_DMAENGINE_PCM` in `soc-devres.c`, which is unconditionally part of `snd-soc-core`. Needs preprocessor-level provider conditions, not just Kbuild ones |

| a reference guarded by an option the query does not constrain (S4) | the solver produces a witness, the minimiser turns the guard off because nothing requires it, and the reference disappears from the compiled object | the pre-check catches it for one compile instead of a link; four of the benchmark's misses are this. The reference condition should carry the guard, which is what `kreach s4 --use-feedback` is for |

Out of scope by design: missing `EXPORT_SYMBOL`, namespace imports and GPL-only
export mismatches, which modpost already reports.

Measured against the historical-recall benchmark, these cost 33 usable cases
**14 hits, recall 0.42** — and the breakdown matters more than the figure. The
largest identified cause of a miss is the S1 conditional-default gap, at three
cases; four are the guard problem in the row above. Separately, **16 of the 26
excluded cases are the y/n case**, which the first row of this table states as
out of scope: most of what the benchmark offers is not what v1 checks. The
diagnosis of every miss is in [../results/benchmark/](../results/benchmark/).
