# SPDX-License-Identifier: GPL-2.0-or-later
"""Diagnostic: why does K forbid a literal that randconfig produced?"""
import sys
import z3
from kreach.config import Context
from kreach.s1_kconfig import load_s1
from kreach import formula as F

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
model = load_s1(ctx)
syms = sys.argv[4:]

print("constraints:", len(model.constraints))
for sym in syms:
    print("\n=== %s  type=%r" % (sym, model.kconfig_types.get(sym)))
    for lit, name in ((F.enabled_var(sym), "enabled"),
                      (F.is_y(sym), "==y"), (F.is_m(sym), "==m")):
        s = z3.Solver(); s.add(*model.constraints)
        print("   %-8s -> %s" % (name, s.check(lit)))
    # which clauses mention it?
    want = {sym, "%s==y" % sym, "%s==m" % sym}
    hits = []
    for c in model.constraints:
        names = {str(v) for v in z3.z3util.get_vars(c)}
        if names & want:
            hits.append(c)
    print("   clauses mentioning it: %d" % len(hits))
    for c in hits[:8]:
        print("     ", str(c).replace("\n", " ")[:300])
