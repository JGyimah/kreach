import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import load_s1
from kreach import formula as F

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
m = load_s1(ctx)
s = z3.Solver(); s.set("timeout", 300000)
s.add(*m.constraints)
print("K alone:", s.check(), flush=True)
for sym in sys.argv[4:]:
    r = s.check(F.enabled_var(sym))
    print("%-40s enabled -> %s" % (sym, r), flush=True)
    if r == z3.unsat:
        print("   core:", [str(c) for c in s.unsat_core()], flush=True)
    # also as a hard assertion in a fresh solver
    s2 = z3.Solver(); s2.set("timeout", 300000)
    s2.add(*m.constraints); s2.add(F.enabled_var(sym))
    print("%-40s hard    -> %s" % (sym, s2.check()), flush=True)
