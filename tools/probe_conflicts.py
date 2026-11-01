# SPDX-License-Identifier: GPL-2.0-or-later
"""Collect every literal of a real .config that K rejects, by iterating cores."""
import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import load_s1, parse_config_file, config_to_assignment

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
cfg = sys.argv[4]
m = load_s1(ctx)
settings = parse_config_file(cfg)
lits = config_to_assignment(settings, m)
s = z3.Solver(); s.set("timeout", 300000)
s.add(*m.constraints)

dropped = []
active = list(lits)
for it in range(60):
    r = s.check(*active)
    if r != z3.unsat:
        print("satisfiable after dropping %d literals (round %d, %s)" % (len(dropped), it, r))
        break
    core = {str(c) for c in s.unsat_core()}
    if not core:
        print("empty core, giving up"); break
    dropped.extend(sorted(core))
    active = [l for l in active if str(l) not in core]
else:
    print("still unsat after 60 rounds")

print("\nrejected literals (%d):" % len(dropped))
for d in dropped:
    print("  ", d)
