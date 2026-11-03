# tools/

Diagnostics and drivers that are not part of the library.

| | |
|---|---|
| `probe_s1.py` | why K forbids a literal: per-symbol satisfiability and the clauses mentioning it |
| `probe_sat.py` | is K satisfiable, and which constraint group makes it unsat |
| `probe_lit.py` | check single literals against K, with the unsat core |
| `probe_conflicts.py` | every literal of a real `.config` that K rejects, by iterating unsat cores |
| `probe_clauses.py` | the per-variable kclause clauses and reverse dependencies |
| `probe_clamp.py` | the dependency expression and R6 clamp for a symbol |
| `probe_r6.py` | per randconfig seed: is K violated, and do the R6 clamps alone reject it |
| `verify_fix.py` | does the suggested fix make the witness link? applies `select <provider>` to each caller's stanza, hands the witness back to Kconfig and links again -- the check kreach itself does not do, since it confirms a finding by a build that *fails*. `--regression` then applies every fix at once and checks the stock configurations plus an allmodconfig `vmlinux` link |
| `bench_replicate.sh` | historical recall for one benchmark case: run the pipeline at a fix's parent commit and score it |
| `bench_worklist.py` | which benchmark cases remain, and what S3 must build for each: the scope follows the symbols the fix names rather than the Kconfig file it edited, because those are not always in the same subsystem |
| `bench_report.py` | the sweep's aggregate, and with `--triage` the misses grouped by what the saved diagnostics say stopped each one: hits by how they matched, misses with reasons, and exclusions counted apart so they do not land in the recall denominator |
| `bench_sweep.sh` | the historical-recall benchmark over a worklist, one case at a time: per-case work directory, deleted once scored, so disk stays flat; resumable, since a case with a score file is skipped |
| `reconfirm_tip.sh` | re-confirm a known set of findings against whatever the tree is checked out at, in its own work directory, with S2 scoped to the subtrees the providers live in |

The probes were written while diagnosing the S1 repairs and are kept because
each one answers a question that comes back whenever the Kconfig encoding
changes. `docs/design.md` says which finding came from which probe.
