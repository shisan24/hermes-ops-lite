# -*- coding: utf-8 -*-
"""预算影子分析：不动运行时，只看「如果按工具收紧预算会多省多少、会不会切掉真被读的东西」。

用法:
  python budget_shadow.py [savings.jsonl] [--factors 0.8,0.6,0.4]

口径：
  * 只统计 bounded 行；repeat 行不是预算问题，retrieve 行是「有人回读」的信号。
  * 额外节省 = max(0, 实际返回字符 - 候选预算)；候选用当前该工具预算乘系数。
  * 风险行 = 该结果后来被回读过（sha1 命中 retrieve），收紧会切掉模型真正去拿的内容。
只读，不写账本、不改配置。
"""
import importlib.util
import json
import pathlib
import sys

_sys = sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
if not (ROOT / "bounding.py").exists():  # 在插件安装目录里跑，而不是仓库根
    ROOT = pathlib.Path.home() / "AppData/Local/hermes/plugins/ops-lite"
spec = importlib.util.spec_from_file_location("bl_shadow", ROOT / "bounding.py")
bl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bl)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
factors_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--factors")), "0.8,0.6,0.4")
factors = [float(f) for f in factors_arg.split(",") if f.strip()]

default = pathlib.Path.home() / "AppData/Local/hermes/plugin-data/ops-lite/savings.jsonl"
ledger = pathlib.Path(args[0]) if args else default
rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()] if ledger.exists() else []
if not rows:
    print("账本为空：", ledger); raise SystemExit(0)

cfg = bl.resolve_config(None)
def budget_of(row):
    return bl.budget_for(cfg, str(row.get("tool_name") or ""), is_error=bool(row.get("error")))

retrieved = {r.get("sha1") for r in rows if r.get("mode") == "retrieve" and r.get("sha1")}
bounded = [r for r in rows if r.get("mode") == "bounded"]
# 有些 JSON 文档的骨架（键名、结构）本身就超过预算，压不动 —— 这类行再收紧预算也不会变，
# 必须排除在「多省」之外，否则影子数字会被它们撑大好几倍。
over = [r for r in bounded if int(r.get("returned_chars") or 0) > budget_of(r)]
compressible = [r for r in bounded if r not in over]
print(f"账本：{ledger}")
print(f"bounded {len(bounded)} 行：可再压 {len(compressible)} 行，已超预算压不动 {len(over)} 行，被回读 {sum(1 for r in bounded if r.get('sha1') in retrieved)} 行\n")


by_tool = {}
for row in compressible:
    by_tool.setdefault(str(row.get("tool_name") or "?"), []).append(row)

print(f"{'工具':28s} {'条数':>5s} {'当前预算':>8s} {'实际返回':>9s} " +
      " ".join(f"{'×' + str(f):>12s}" for f in factors) + "  风险行")
totals = {f: 0.0 for f in factors}
for tool, items in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
    budget = budget_of(items[0])
    returned = sum(int(r.get("returned_chars") or 0) for r in items)
    line = f"{tool[:28]:28s} {len(items):5d} {budget:8,d} {returned:9,d} "
    for f in factors:
        candidate = int(budget * f)
        extra = sum(max(0, int(r.get("returned_chars") or 0) - candidate) for r in items)
        totals[f] += extra
        line += f"{extra:12,d} "
    risk = sum(1 for r in items if r.get("sha1") in retrieved)
    line += f" {risk:6d}"
    print(line)

print("\n合计可多省（字符，未折算 token）：")
for f in factors:
    print(f"  预算 ×{f:<4} → {totals[f]:>10,.0f} 字符 ≈ {totals[f]/3.6:>9,.0f} tok（一次性，未放大）")

print("\n注意：只统计当前确实压到预算内的行；额外节省成立的前提是那些结果没被回读，风险行就是反例。")
