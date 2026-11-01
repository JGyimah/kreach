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
| `bench_replicate.sh` | historical recall for one benchmark case: run the pipeline at a fix's parent commit and score it |

The probes were written while diagnosing the S1 repairs and are kept because
each one answers a question that comes back whenever the Kconfig encoding
changes. `docs/design.md` says which finding came from which probe.
