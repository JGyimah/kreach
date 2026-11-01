import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import _make_arch
from kreach import formula as F

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
arch = _make_arch(ctx, verify=False)
arch.load_kextract(ctx.kextract_file)
kc = arch.get_kclause()          # var -> [smt2 clause strings]
rd = arch.get_rev_dep()          # var -> smt2 rev-dep
print("kclause vars: %d, rev_dep vars: %d" % (len(kc), len(rd)))
for sym in sys.argv[4:]:
    print("\n==== %s" % sym)
    print("  rev_dep:", str(F.conj(z3.parse_smt2_string(rd[sym])) if sym in rd else None)[:300].replace("\n"," "))
    for i, cl in enumerate(kc.get(sym, [])):
        e = F.conj(z3.parse_smt2_string(cl))
        print("  clause[%d]: %s" % (i, str(e)[:400].replace("\n", " ")))
