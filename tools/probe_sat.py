# SPDX-License-Identifier: GPL-2.0-or-later
"""Is K satisfiable?  If not, which group of constraints makes it unsat?"""
import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import _make_arch
from kreach import formula as F

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
arch = _make_arch(ctx, verify=False)
arch.load_kextract(ctx.kextract_file)
kclause = z3.parse_smt2_string(open(ctx.kclause_file).read())
types = dict(arch.get_kconfig_types() or {})
archc = list(arch.get_arch_specific_constraints())
compl = F.completion_constraints(types)
modoff = F.modules_off_forces_no_m(types)

groups = [("kclause", kclause), ("arch", archc),
          ("completion", compl), ("modules_off", modoff)]
acc = []
for name, g in groups:
    acc = acc + list(g)
    s = z3.Solver(); s.set("timeout", 180000); s.add(*acc)
    print("+%-12s (%6d clauses, total %6d) -> %s" % (name, len(g), len(acc), s.check()), flush=True)
