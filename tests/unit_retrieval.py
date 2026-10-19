# -*- coding: utf-8 -*-
"""ops-lite 取回侦测端到端测试（bound_result 全链路，带最小 Hermes 桩）。

用法: python unit_retrieval.py [plugins/ops-lite 目录]
在临时包里加载插件源码，桩掉 plugins.plugin_storage 与 hermes_cli.config，
然后：① 收敛一个大结果（应写 bounded 行 + 记住提示）② 模拟"按提示回读"
③ 断言账本出现对应 retrieve 行（留档/专档/原文件三条路径）④ 无关读取不得误记。
"""

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 控制台代码页不保证 UTF-8
import importlib.util, json, pathlib, shutil, sys, tempfile, types

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
TMP = pathlib.Path(tempfile.mkdtemp())
PKG = TMP / "opslite_pkg"
PKG.mkdir()
for name in ("__init__.py", "bounding.py"):
    shutil.copy2(SRC / name, PKG / name)

# --- 最小桩：插件数据目录 + 配置 ---
data = TMP / "data"
store = types.ModuleType("plugins.plugin_storage")
store.plugin_data_dir = lambda name: data / name
plugins_pkg = types.ModuleType("plugins")
plugins_pkg.plugin_storage = store
sys.modules["plugins"] = plugins_pkg
sys.modules["plugins.plugin_storage"] = store

sys.path.insert(0, str(TMP))
import opslite_pkg as P  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def ledger():
    path = data / "ops-lite" / "savings.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


# ---------- ① 收敛一个大结果 ----------
big = json.dumps({"output": "\n".join(f"log{i}: {'y' * 40}" for i in range(1, 1201)), "exit_code": 0},
                 ensure_ascii=False)
marker = P.bound_result({"tool_name": "terminal", "args": {"command": "run"}, "result": big,
                         "session_id": "s1"})
check("大结果被收敛", bool(marker) and len(marker) < len(big), f"{len(big):,}→{len(marker or ''):,}")
rows = ledger()
bounded = [r for r in rows if r.get("mode") == "bounded"]
check("账本写入 bounded 行", len(bounded) == 1 and bounded[0]["sha1"], f"sha1={bounded[0]['sha1'] if bounded else '-'}")
sha = bounded[0]["sha1"]
hint = str(bounded[0].get("retrieve") or "")
print("     提示:", hint[:120])
sidecar = (data / "ops-lite" / "spill" / f"{sha}.field.txt")
check("提示指向字段专档", f"{sha}.field.txt" in hint and sidecar.exists(), sidecar.name)
careful_offset = int(hint.split("offset=")[1].split(",")[0]) if "offset=" in hint else 1

# ---------- ② 模拟按提示回读（专档） ----------
P.bound_result({"tool_name": "read_file", "args": {"path": str(sidecar), "offset": careful_offset, "limit": 200},
                "result": "log42: yyyy", "session_id": "s1"})
got = [r for r in ledger() if r.get("mode") == "retrieve"]
check("专档回读被记为 retrieve/sidecar", len(got) == 1 and got[0].get("via") == "sidecar" and got[0].get("sha1") == sha,
      json.dumps(got[:1], ensure_ascii=False)[:160])

# ---------- ③ search_files 蹲留档本体 ----------
spill_file = data / "ops-lite" / "spill" / f"{sha}.txt"
P.bound_result({"tool_name": "search_files", "args": {"pattern": "log42", "path": str(spill_file)},
                "result": "log42: yyyy", "session_id": "s1"})
got = [r for r in ledger() if r.get("mode") == "retrieve"]
check("留档本体被 grep 记为 retrieve/spill", len(got) == 2 and got[1].get("via") == "spill", f"{len(got)} 行")

# ---------- ④ 源文件回读（按 path+offset 匹配） ----------
src_file = TMP / "src.txt"
src_file.write_text("\n".join(f"L{i}-{'x' * 30}" for i in range(1, 2001)), encoding="utf-8")
content = "\n".join(f"L{i}-{'x' * 30}" for i in range(500, 1500))
P.bound_result({"tool_name": "read_file", "args": {"path": str(src_file), "offset": 500, "limit": 1000},
                "result": json.dumps({"content": content, "total_lines": 2000, "path": str(src_file)}, ensure_ascii=False),
                "session_id": "s1"})
b2 = [r for r in ledger() if r.get("mode") == "bounded"][-1]
hint2 = str(b2.get("retrieve") or "")
off2 = int(hint2.split("offset=")[1].split(",")[0])
P.bound_result({"tool_name": "read_file", "args": {"path": str(src_file), "offset": off2, "limit": 200},
                "result": "L561-xxx", "session_id": "s1"})
got = [r for r in ledger() if r.get("mode") == "retrieve"]
check("源文件按提示 offset 回读记为 retrieve/source",
      any(r.get("via") == "source" and r.get("sha1") == b2["sha1"] for r in got), f"提示 offset={off2}")

# ---------- ⑤ 无关读取不得误记 ----------
before = len([r for r in ledger() if r.get("mode") == "retrieve"])
P.bound_result({"tool_name": "read_file", "args": {"path": str(src_file), "offset": 1, "limit": 200},
                "result": "L1-xxx", "session_id": "s1"})
P.bound_result({"tool_name": "read_file", "args": {"path": str(TMP / "other.txt")}, "result": "x", "session_id": "s1"})
after = len([r for r in ledger() if r.get("mode") == "retrieve"])
check("开头的重读与其他文件不计入取回", after == before, f"{before}→{after}")

# ---------- ⑥ 开关 ----------
P._config_cache = (0.0, {})
_off = dict(P.bounding.DEFAULTS); _off["retrieve_log"] = False; _off["max_chars"] = 100
P._config = lambda: _off                                             # 完整配置，只关掉侦测
n0 = len(ledger())
P.bound_result({"tool_name": "read_file", "args": {"path": str(sidecar)}, "result": "x", "session_id": "s1"})
check("retrieve_log=false 时不记账", len(ledger()) == n0)

print(f"\n=== {len(FAILS)} 项失败" if FAILS else "\n=== 全部通过")
sys.exit(1 if FAILS else 0)
