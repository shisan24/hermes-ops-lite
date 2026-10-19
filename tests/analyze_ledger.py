# -*- coding: utf-8 -*-
"""一条命令出收益表：读 ops-lite 账本（必要时结合 state.db 算放大倍数）。

用法:
  python analyze_ledger.py                 # 用本机默认路径
  python analyze_ledger.py <savings.jsonl> [state.db]

输出：收敛次数、一次性省量、按后续重发放大的估算、按工具分布、取回率（按 via 分类）、
      近 7 天趋势。只读，不写任何东西。放大口径：每次收敛之后该会话还有多少次 assistant
      消息（= 还要重发多少遍这段缩短后的上下文），token ≈ 字符 / 3.6（下限估计）。
"""

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 控制台代码页不保证 UTF-8
import collections, datetime, json, pathlib, sqlite3, sys, time

HOME = pathlib.Path.home() / "AppData/Local/hermes"
LEDGER = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HOME / "plugin-data/ops-lite/savings.jsonl"
STATE = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else HOME / "state.db"
rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()] if LEDGER.exists() else []
if not rows:
    print("账本为空：", LEDGER); raise SystemExit(0)

# --- 放大倍数（有 state.db 才算，没有就按 1 计） ---
future: dict[str, list[float]] = {}
try:
    db = sqlite3.connect(f"file:{STATE}?mode=ro", uri=True)
    for sid, ts in db.execute("SELECT session_id, timestamp FROM messages WHERE role='assistant'"):
        try: future.setdefault(str(sid), []).append(float(ts))
        except (TypeError, ValueError): pass
    db.close()
except Exception as exc:
    print(f"（state.db 不可读，放大倍数按 1 计：{exc}）")

bounded = [r for r in rows if r.get("mode") == "bounded"]
repeat = [r for r in rows if r.get("mode") == "repeat"]
got = {r.get("sha1") for r in rows if r.get("mode") == "retrieve"}
saved_once = sum(int(r.get("saved_chars") or 0) for r in rows)
amplified = 0.0
for r in rows:
    ts = r.get("ts")
    try: ts = float(ts)
    except (TypeError, ValueError): continue
    marks = future.get(str(r.get("session_id") or ""), [])
    mult = 1 + len([t for t in marks if t > ts]) if marks else 1
    amplified += int(r.get("saved_chars") or 0) / 3.6 * mult

print(f"账本：{LEDGER}")
print(f"  收敛 {len(bounded) + len(repeat):,} 次（超预算 {len(bounded)}，重复短路 {len(repeat)}）")
print(f"  一次性省下 {saved_once:,} 字符 ≈ {saved_once / 3.6:,.0f} tok")
print(f"  按后续重发放大 ≈ {amplified:,.0f} tok（下限估计；字符/3.6，助手消息数当重发轮数）")
if bounded:
    hit = len({r.get("sha1") for r in bounded} & got)
    total = len({r.get("sha1") for r in bounded})
    note = "" if hit else "（自动统计自 2026-10-19 起；此前人工反查约 19%，不是 0）"
    print(f"  取回率：{hit}/{total} 次（{100 * hit / total:.0f}%）{note}")
    via = collections.Counter(r.get("via") for r in rows if r.get("mode") == "retrieve")
    if via:
        print("  取回方式：", dict(via))
effects = [r for r in rows if r.get("mode") == "effect"]
if effects:
    states = collections.Counter(str(r.get("state") or "unknown") for r in effects)
    print(f"  效果判定 {len(effects):,} 次：" + " · ".join(f"{k} {v}" for k, v in states.most_common()))
    for r in effects[-5:]:
        ts = datetime.datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%m-%d %H:%M")
        print(f"    {ts}  {str(r.get('tool_name'))[:14]:14s} {str(r.get('state')):8s} {(r.get('evidence') or '—')[:70]}")
by_tool = collections.Counter()
for r in bounded + repeat:
    by_tool[str(r.get("tool_name") or "?")] += int(r.get("saved_chars") or 0)
print("  按工具（节省字符）：")
for name, value in by_tool.most_common(8):
    print(f"    {name:<30} {value:>10,}")
week = collections.Counter()
for r in bounded + repeat:
    try: week[datetime.date.fromtimestamp(float(r["ts"])).isoformat()] += int(r.get("saved_chars") or 0)
    except Exception: pass
if week:
    print("  近 7 天（省字符）：")
    for day, value in sorted(week.items())[-7:]:
        print(f"    {day}  {value:>10,}")
