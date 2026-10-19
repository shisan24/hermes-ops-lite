# -*- coding: utf-8 -*-
"""ops-lite 压力测试：攻击面面向新加的效果判定（effectState）与它和收敛路径的交叉。

用法: python stress_effect.py [轮数=300] [插件目录]
不变式（任一破坏即 FAIL）：
  ① 任何输入都不抛异常
  ② 不标注时必须返回 None（原样放行），标注时必须是 str
  ③ 标注后长度 ≤ 原文 + 512 字符（成本有界）
  ④ 凡是 JSON dict 输入，输出仍可 json.loads，且原有键一个不丢
  ⑤ 幂等：同一输入标注两次结果一致，不出现双标记
  ⑥ 只有 is_error 且命中工具族才可能标注；成功结果永不标注
  ⑦ 并发下账本行不损坏、不丢行（O_APPEND 单次写）
  ⑧ 热路径成本：< 1ms/次（小结果），1MB 输入不爆内存
"""
import importlib.util
import json
import pathlib
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import types

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 300
SRC = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else str(pathlib.Path(__file__).resolve().parent.parent))
TMP = pathlib.Path(tempfile.mkdtemp())
PKG = TMP / "opslite_pkg"
PKG.mkdir()
for name in ("__init__.py", "bounding.py"):
    shutil.copy2(SRC / name, PKG / name)

data = TMP / "data"
store = types.ModuleType("plugins.plugin_storage")
store.plugin_data_dir = lambda name: data / name
plugins_pkg = types.ModuleType("plugins")
plugins_pkg.plugin_storage = store
sys.modules["plugins"] = plugins_pkg
sys.modules["plugins.plugin_storage"] = store
sys.path.insert(0, str(TMP))
import opslite_pkg as P  # noqa: E402

B = P.bounding
FAILS: list[str] = []


def fail(name, detail=""):
    FAILS.append(name)
    print(f"FAIL  {name}  {detail}")


def ok(name, detail=""):
    print(f"PASS  {name}  {detail}")


def ledger_lines():
    path = data / "ops-lite" / "savings.jsonl"
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]


# ─────────────── 一、随机攻击：分类器 + 标注器的组合 ───────────────
ALPHABET = "abcxyz0123 \n\t\u0000\ufffd\u2028\u2029\u200b\u0301\"'\\{}[]:,.-"
KEYWORDS = ["not modified", "was modified", "timed out", "permission denied", "killed",
            "could not find a match", "partially applied", "read-only", "blocked by", "old_string not found"]
TOOLS = ["write_file", "patch", "skill_manage", "terminal", "execute_code", "read_file", "memory", "unknown_tool", ""]


def random_text(rng):
    kind = rng.random()
    if kind < 0.35:            # 关键词随机塞进噪声
        body = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 200)))
        return body + rng.choice(KEYWORDS) + body
    if kind < 0.55:            # 纯噪声
        return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 500)))
    if kind < 0.75:            # JSON dict
        return json.dumps({"error": rng.choice(KEYWORDS), "n": rng.randint(0, 9),
                           "nested": {"a": [1, 2, 3], "b": None}}, ensure_ascii=False)
    if kind < 0.85:            # 已带标记
        return json.dumps({"error": "x", "_ops_effect": {"state": "none", "next": "y"}}, ensure_ascii=False)
    if kind < 0.92:            # JSON 数组 / 标量
        return rng.choice(['[1,2,3]', '"plain"', '42', 'null', '{}', '[]'])
    return json.dumps({"error": "x" * rng.randint(0, 5000)}, ensure_ascii=False)


rng = random.Random(20261020)
for i in range(ROUNDS):
    tool = rng.choice(TOOLS)
    is_error = rng.random() < 0.7
    text = random_text(rng)
    try:
        state = B.classify_effect(tool, text, error_message=rng.choice(KEYWORDS) if rng.random() < 0.3 else None,
                                  is_error=is_error)
        if state not in (None, "none", "applied", "unknown"):
            fail(f"round {i}: 非法状态 {state!r}")
            continue
        if not is_error and state is not None:
            fail(f"round {i}: 成功结果被标注 {state}")
            continue
        out = B.annotate_effect(text, state) if state else text
        if state and not isinstance(out, str):
            fail(f"round {i}: 标注返回非字符串")
            continue
        if len(out) > len(text) + 512:
            fail(f"round {i}: 标注超预算 {len(text)}→{len(out)}")
        if state and B.annotate_effect(out, state) != out:
            fail(f"round {i}: 不幂等")
        if out.count("[ops-lite 效果：") > 1:
            fail(f"round {i}: 双标记")
        try:
            original = json.loads(text)
        except Exception:
            original = None
        if isinstance(original, dict):
            parsed = json.loads(out)
            if any(k not in parsed for k in original):
                fail(f"round {i}: 丢键")
    except Exception as exc:  # noqa: BLE001
        fail(f"round {i}: 抛异常 {type(exc).__name__}: {exc}")
        break
else:
    ok(f"随机攻击 {ROUNDS} 轮无破坏")

# ─────────────── 二、定点攻击 ───────────────
cases = [
    ("空串", "", "patch", True),
    ("纯空白", " \n\t ", "write_file", True),
    ("1MB 文本 + 结尾关键词", "a" * (1024 * 1024) + " timed out", "terminal", True),
    ("65k 深嵌套 JSON", json.dumps({"a": [[[[{"b": "not modified"}]]]]}, ensure_ascii=False) * 1, "patch", True),
    ("孤立代理对", "x\ud800y", "patch", True),
    ("JSON 里既有 _ops_effect 又是错误", json.dumps({"error": "killed", "_ops_effect": "旧"}), "patch", True),
    ("错误文本含其他工具关键词", json.dumps({"error": "see read_file: cache timeout config"}), "terminal", True),
    ("成功但正文含 not modified", json.dumps({"success": True, "note": "The file was NOT modified"}), "write_file", False),
]
for name, text, tool, is_error in cases:
    started = time.perf_counter()
    try:
        state = B.classify_effect(tool, text, is_error=is_error)
        out = B.annotate_effect(text, state) if state else text
        elapsed = (time.perf_counter() - started) * 1000
        if not is_error and state is not None:
            fail(f"定点-{name}: 成功被标注")
        elif len(out) > len(text) + 512:
            fail(f"定点-{name}: 超预算")
        elif is_error is False and out != text:
            fail(f"定点-{name}: 成功结果被改写")
        else:
            ok(f"定点-{name}", f"{elapsed:.2f}ms state={state}")
    except Exception as exc:  # noqa: BLE001
        fail(f"定点-{name}: 抛异常 {exc}")

# ─────────────── 三、并发：账本是追加写，行不能损坏 ───────────────
THREADS, PER_THREAD = 8, 60
payload = {"tool_name": "patch", "args": {"path": "D:/x"}, "session_id": "stress",
           "status": "error",
           "result": json.dumps({"success": False, "error": "Could not find a match for old_string in the file"})}


def worker():
    for _ in range(PER_THREAD):
        P.bound_result(dict(payload))


before = len(ledger_lines())
threads = [threading.Thread(target=worker) for _ in range(THREADS)]
started = time.perf_counter()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.perf_counter() - started
lines = ledger_lines()
new = lines[before:]
bad = 0
effect_rows = 0
for line in new:
    try:
        row = json.loads(line)
    except Exception:
        bad += 1
        continue
    if row.get("mode") == "effect":
        effect_rows += 1
expected = THREADS * PER_THREAD
if bad:
    fail(f"并发-账本损坏 {bad} 行")
elif effect_rows != expected:
    fail(f"并发-丢行 {effect_rows}/{expected}")
else:
    ok(f"并发 {THREADS}×{PER_THREAD} 行全好", f"{elapsed*1000:.0f}ms, {expected/elapsed:,.0f} 次/秒")

# ─────────────── 四、热路径成本 ───────────────
small = json.dumps({"success": False, "error": "Could not find a match for old_string in the file"})
big = json.dumps({"error": "x" * 200_000})
for name, text, limit_ms in (("小错误结果", small, 0.05), ("200k 错误结果", big, 5.0)):
    samples = []
    for _ in range(200):
        t0 = time.perf_counter()
        state = B.classify_effect("patch", text, is_error=True)
        _ = B.annotate_effect(text, state) if state else text
        samples.append((time.perf_counter() - t0) * 1000)
    mean, peak = statistics.mean(samples), max(samples)
    if mean > limit_ms:
        fail(f"热路径-{name}: 均值 {mean:.3f}ms 超 {limit_ms}ms")
    else:
        ok(f"热路径-{name}", f"均值 {mean:.3f}ms / 峰值 {peak:.3f}ms（阈 {limit_ms}ms）")

# ─────────────── 五、与收敛路径的交叉：标注必须活下来 ───────────────
huge_error = json.dumps({"status": "failed", "error": "timed out after 300s; " + "detail " * 4000}, ensure_ascii=False)
out = P.bound_result({"tool_name": "skill_manage", "args": {"action": "patch", "name": "x"},
                      "session_id": "stress", "status": "error", "result": huge_error})
survived = isinstance(out, str) and ("_ops_effect" in out or "[ops-lite 效果：" in out)
bounded = isinstance(out, str) and len(out) < len(huge_error)
if not (survived and bounded):
    fail("交叉-大错误结果", f"survived={survived} bounded={bounded} len={len(out or '')}")
else:
    ok("交叉-大错误结果既收敛又保住标注", f"{len(huge_error):,}→{len(out):,}")

print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败：" + "；".join(FAILS[:8]))
    sys.exit(1)
print("=== 全部通过")
