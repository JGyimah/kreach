import sys, z3
from kreach.config import Context
from kreach.s1_kconfig import _make_arch, load_s1
from kreach import formula as F

ctx = Context(ksrc=sys.argv[1], work=sys.argv[2], arch=sys.argv[3])
m = load_s1(ctx)
arch = _make_arch(ctx, verify=False); arch.load_kextract(ctx.kextract_file)
dd = arch.get_dir_dep() or {}
for sym in sys.argv[4:]:
    print("====", sym, "type=", m.kconfig_types.get(sym))
    if sym not in dd:
        print("   no dir_dep"); continue
    dep = F.conj(z3.parse_smt2_string(dd[sym]))
    syms = F.collect_config_symbols([dep])
    tri = {s for s in syms if m.kconfig_types.get(s) == "tristate"}
    print("   dep      :", str(z3.simplify(dep))[:300].replace("\n", " "))
    print("   tristates:", sorted(tri))
    print("   dep_at_y :", str(z3.simplify(F.relabel_builtin(dep, skip=syms - tri)))[:300].replace("\n", " "))
