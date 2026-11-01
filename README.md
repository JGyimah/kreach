# kreach — Kconfig Reachability Checker

kreach finds Kconfig configurations that Kconfig accepts but that fail to link,
because built-in code calls a function provided by code that is a module or
disabled. Each finding comes with a minimal configuration that reproduces the
failure, confirmed by a real build.

The property checked is one line: **whenever an object file references a symbol,
the code providing that symbol must be at least as available as the referencing
code.** Levels are ordered `n < m < y`, and a pair `(caller, provider)` linked by
symbol `s` is a bug when some valid configuration has the reference emitted and
the provider less available than the caller:

```
K ∧ RC(c,s) ∧ level(c) > level(p)
```

The `y`/`m` case is the main target: the caller is in vmlinux, the provider is a
module, and the usual `IS_ENABLED()` header stub does not protect against it
because `IS_ENABLED` is true for `m`.

## Install

```bash
python3 -m venv ~/venv && ~/venv/bin/pip install kmax z3-solver python-sat
~/venv/bin/pip install -e .
```

kreach shells out to the kmax suite (`kextract`, `kclause`, `kmaxall`), and puts
the interpreter's script directory on `PATH` itself, so running it by absolute
path out of a virtualenv works.

Building witnesses needs a kernel build toolchain: `build-essential flex bison
libssl-dev libelf-dev bc`.

## Use

```bash
# one work directory per kernel commit + architecture
export KREACH_KSRC=~/src/linux KREACH_WORK=~/work/x86_64

kreach -a x86_64 s1                    # Kconfig model K
kreach -a x86_64 s2                    # Kbuild presence conditions, whole tree
kreach -a x86_64 s3 -d sound           # symbol references from a wide build
kreach -a x86_64 s4                    # candidate pairs
kreach -a x86_64 s5                    # solve + witness .config files
kreach -a x86_64 s6 --ccache           # build confirmation
kreach -a x86_64 s7                    # report and fix suggestion

kreach -a x86_64 run -d sound          # all of the above
kreach status                          # what has been computed so far
```

S6 is where the time goes, so it has a few knobs: `--ccache` (witness configs
differ by a handful of options, so the cache does most of the work),
`--provider CONFIG_X` to settle one provider's findings first, `--limit`, and
`--parallel`. Results accumulate across runs, so a bounded run now and the
rest later lose nothing. Only one S6 may run per work directory; use a
separate `-w` to build in parallel.

`-d` restricts a stage to a subtree.  It is worth using for S3, whose wide
build is the expensive part, and worth *not* using for S2, which is static.

Stage acceptance tests, which are the real check on the model:

```bash
kreach -a x86_64 test s1 --samples 1000   # randconfig round-trip, both ways
kreach -a x86_64 test s2 -d sound         # predicted object levels vs real builds
```

Evaluation helpers:

```bash
kreach bench --since v6.0              # mine the historical-recall benchmark
kreach score                           # score findings against it
kreach compare old/findings.jsonl      # still present / fixed / new
```

`tools/bench_replicate.sh` runs one benchmark case properly: check out the
parent of a fix, run the pipeline there, and score that commit.

## Pipeline

Seven stages, one feedback loop. S1–S3 run once per architecture and kernel
commit; S4–S6 run per candidate pair. Candidates that S6 rejects feed back into
the rules of S4 (`kreach s4 --use-feedback`).

| Stage | Does | Writes |
|---|---|---|
| S1 | Kconfig tree → formula `K` | `s1/kextract`, `s1/kclause`, `s1/model.smt2`, `s1/vars.json` |
| S2 | per-object presence conditions | `s2/presence.json.gz`, `s2/unmodelled.json` |
| S3 | references and providers from ELF | `s3/symbols.json.gz` |
| S4 | reference conditions, candidates | `s4/candidates.jsonl`, `s4/guarded.jsonl` |
| S5 | solver query, minimal witness | `s5/queries.jsonl`, `s5/witness/*.config` |
| | | (also reads `s1/realisable.smt2`, so witnesses are writable as a `.config`) |
| S6 | build confirmation, accumulating | `s6/results.jsonl`, `s6/build/*/build.log` |
| | two cheap pre-checks before each build: the witness must still satisfy the query, and the caller object must still emit the symbol | |
| S7 | report and fix suggestion | `s7/findings.jsonl`, `s7/findings.txt` |

Each stage reads the output of the one before and writes a file, so stages can be
rerun, cached and tested on their own.

## What is reused

The kmax suite does the parts it already does well, as the spec asks:

- **kextract + kclause** parse Kconfig and encode `depends on`, `select`,
  visibility and choice groups. kreach adds level variables for every tristate
  symbol, the `MODULES=n` linkage, and six repairs where kclause's formula
  differs from Kconfig — most importantly that a `select`ed tristate may be a
  module, and that `depends on` caps a symbol's level. See
  [docs/design.md](docs/design.md) and `kreach/s1_repair.py`.
- **kmax** computes per-object presence conditions, so kreach does not parse
  Makefiles to find objects. kmax's condition says an object is *compiled*;
  splitting that into "built into vmlinux" and "built at all" is kreach's job.

S2 is static, so it can cover the whole tree even when only one subsystem has
been built: callers come from wherever there are binaries, providers from
anywhere. Its default scope is the main code directories plus
`arch/<SRCARCH>/`, because an architecture's own code provides symbols too --
leaving it out makes S4 pick a same-named object elsewhere in the tree
(`lib/hweight.o` instead of `arch/x86/lib/hweight.o`).

## Findings from a real run

[results/](results/) holds the output of a run on Linux v6.17, `ARCH=x86_64`,
with `sound/` as the caller subsystem: **eleven findings**, each confirmed by a
`vmlinux` link that failed on the predicted symbol, each with the minimised
`.config` that reproduces it. [results/README.md](results/README.md) lists them
with their `Fixes:` commits and how to reproduce one.

## Known limitations

kreach can miss bugs but never reports an unconfirmed one: build confirmation
removes false positives, so every limitation costs recall, not precision. See
[docs/design.md](docs/design.md) for the measured state of each, and
[docs/results.md](docs/results.md) for a full run on v6.17 x86_64 with the
numbers each stage produced.

## Layout

```
kreach/
  formula.py       tristate algebra: level variables, the relabelling, the three cases
  s1_kconfig.py    S1
  s1_repair.py     the five S1 repairs, with the reason for each
  s2_presence.py   S2
  s3_symbols.py    S3
  s4_refcond.py    S4
  s5_solve.py      S5
  s6_confirm.py    S6
  s7_report.py     S7
  acceptance.py    S1 and S2 acceptance tests
  benchmark.py     historical-recall benchmark from git history
  cli.py           command line
tests/
  test_formula.py      the tristate algebra and the three failure cases
  test_s1_repair.py    each S1 repair against the Kconfig rule it encodes
  test_presence.py     S2/S3: the Kbuild shapes the acceptance test turned up
  test_candidates.py   S4/S5/S6: guard scan, query identity, build-log reading
  test_report.py       S7: fix choice, grouping, the commit-message block
  test_benchmark.py    mining a fix commit and scoring findings against it
  test_cli.py          the stage commands and `kreach run`'s argument namespace
tools/             diagnostics used while building the S1 repairs, and a
                   driver for one historical-recall benchmark case
```
