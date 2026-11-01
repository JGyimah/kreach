# SPDX-License-Identifier: GPL-2.0-or-later
"""For each randconfig seed: is K violated, and are the R6 clamps alone violated?"""
import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import _make_arch, load_s1, parse_config_file, config_to_assignment
from kreach.s1_repair import KextractFacts, dependency_level_clamps
from kreach.s3_symbols import make

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
seeds = [int(x) for x in sys.argv[4:]] or [1]
m = load_s1(ctx)
arch = _make_arch(ctx, verify=False); arch.load_kextract(ctx.kextract_file)
facts = KextractFacts(open(ctx.kextract_file).read())
clamps = dependency_level_clamps(facts, m.kconfig_types)
print("R6 clamps:", len(clamps), flush=True)

bdir = ctx.dir("accept", "probe")
sK = z3.Solver(); sK.set("timeout", 120000); sK.add(*m.constraints)
sR = z3.Solver(); sR.set("timeout", 120000); sR.add(*clamps)
for seed in seeds:
    make(ctx, bdir, ["randconfig"], jobs=1, extra_env={"KCONFIG_SEED": hex(seed)}, check=False)
    cfg = bdir + "/.config"
    lits = config_to_assignment(parse_config_file(cfg), m)
    rk = sK.check(*lits)
    coreK = sorted({str(c) for c in sK.unsat_core()})[:4] if rk == z3.unsat else []
    rr = sR.check(*lits)
    coreR = sorted({str(c) for c in sR.unsat_core()})[:4] if rr == z3.unsat else []
    print("seed %2d  K=%-6s R6only=%-6s  K_core=%s  R6_core=%s"
          % (seed, rk, rr, coreK, coreR), flush=True)
