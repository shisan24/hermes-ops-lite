# -*- coding: utf-8 -*-
"""ops-lite 效果判定（effectState）测试：纯分类 + 端到端钩子链路。

用法: python unit_effect.py [plugins/ops-lite 目录]
判定口径来自 dsh-ops 的 Failure.effectState（none / applied / unknown）：
失败的写操作必须告诉模型「到底动了没有」，因为这一步决定能不能重试。

不变量：
  ① write_file / patch 在 SKIP_TOOLS 里不能被当成「跳过」而吞掉效果标注
  ② 非错误的成功结果绝不标注（也没有必要的 token 开销）
  ③ 只读工具的错误不标注
  ④ shell 只在被截断/超时/杀进程时标 unknown，普通非零退出不标注（不喊狼来了）
  ⑤ JSON 信封加 _ops_effect 键后仍可 json.loads，原有键一个不丢
  ⑥ 标注幂等
"""
import importlib.util, json, pathlib, shutil, sys, tempfile, types

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
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


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def ledger():
    path = data / "ops-lite" / "savings.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


# ---------- 一、纯分类 ----------
check("patch 找不到 old_string → none",
      B.classify_effect("patch", '{"success": false, "error": "Could not find a match for old_string in the file"}',
                        is_error=True) == "none")
check("write_file 拒绝覆盖 → none",
      B.classify_effect("write_file", '{"error": "Refusing to overwrite D:\\x: ... The file was NOT modified."}',
                        is_error=True) == "none")
check("permission denied → none",
      B.classify_effect("write_file", "open: permission denied", is_error=True) == "none")
check("写后才失败 → applied",
      B.classify_effect("patch", "file was modified but the follow-up verification failed", is_error=True) == "applied")
check("NOT modified 不会被误判成 applied",
      B.classify_effect("patch", "The file was NOT modified.", is_error=True) == "none")
check("未识别的写错误 → unknown（保守）",
      B.classify_effect("write_file", "something strange happened", is_error=True) == "unknown")
check("terminal 超时 → unknown",
      B.classify_effect("terminal", "Command timed out after 300s", is_error=True) == "unknown")
check("terminal 普通非零退出不标注",
      B.classify_effect("terminal", '{"output": "grep: no match", "exit_code": 1}', is_error=True) is None)
check("只读工具错误不标注",
      B.classify_effect("read_file", "permission denied", is_error=True) is None)
check("成功结果不标注",
      B.classify_effect("write_file", '{"bytes_written": 12}', is_error=False) is None)
check("大小写与 error_message 都参与判定",
      B.classify_effect("terminal", "", error_message="PROCESS KILLED", is_error=True) == "unknown")

# ---- 判据可回溯：每条判定都要带出「凭什么」 ----
state, why = B.classify_effect_detail("patch", '{"success": false, "error": "Could not find a match for old_string in the file"}', is_error=True)
check("none 带命中短语", state == "none" and why == "could not find a match", f"{state} / {why!r}")
state, why = B.classify_effect_detail("terminal", "Command timed out after 300s", is_error=True)
check("unknown 带命中短语", state == "unknown" and why == "timed out", f"{state} / {why!r}")
state, why = B.classify_effect_detail("write_file", "some brand new failure wording", is_error=True)
check("未命中时留下 no-pattern + 原文", state == "unknown" and (why or "").startswith("no-pattern: some brand new"),
      f"{state} / {why!r}")
check("成功结果不给判据", B.classify_effect_detail("write_file", "x", is_error=False) == (None, None))

# ---------- 二、标注形式 ----------
raw = json.dumps({"success": False, "error": "Could not find a match for old_string in the file", "_hint": "x"},
                 ensure_ascii=False)
annotated = B.annotate_effect(raw, "none")
parsed = json.loads(annotated)
check("JSON 信封仍可解析", isinstance(parsed, dict), annotated[:80])
check("_ops_effect 状态正确", parsed.get("_ops_effect", {}).get("state") == "none")
check("原有键一个不丢", parsed.get("error") == "Could not find a match for old_string in the file" and parsed.get("_hint") == "x")
check("标注幂等", B.annotate_effect(annotated, "none") == annotated)
text_out = B.annotate_effect("boom: timed out", "unknown")
check("纯文本追加一行", text_out.endswith("]") and "[ops-lite 效果：unknown" in text_out, text_out.splitlines()[-1][:60])
check("成本有界（≤ 原文 + 512）", len(annotated) <= len(raw) + 512, f"{len(raw)}→{len(annotated)}")

# ---------- 三、端到端：钩子链路 ----------
patch_err = json.dumps({"success": False, "error": "Could not find a match for old_string in the file"}, ensure_ascii=False)
out = P.bound_result({"tool_name": "patch", "args": {"path": "D:/x.txt"}, "result": patch_err,
                      "session_id": "s1", "status": "error", "error_type": "tool_error"})
check("patch 错误被标注并返回（SKIP_TOOLS 不吞）", isinstance(out, str) and "_ops_effect" in out, str(out)[:80])
rows = [r for r in ledger() if r.get("mode") == "effect"]
check("账本写入 effect 行", len(rows) == 1 and rows[0].get("state") == "none" and rows[0].get("tool_name") == "patch")
check("账本带回判据", (rows[0].get("evidence") or "") == "could not find a match", rows[0].get("evidence") if rows else None)
check("effect 行不污染节省统计", (rows[0].get("saved_chars") or 0) == 0)

wf_err = json.dumps({"error": "The file was NOT modified.", "stale_write_blocked": True}, ensure_ascii=False)
out = P.bound_result({"tool_name": "write_file", "args": {"path": "D:/x.txt"}, "result": wf_err,
                      "session_id": "s1", "status": "error"})
check("write_file 错误也被标注", isinstance(out, str) and "none" in out)

ok = P.bound_result({"tool_name": "write_file", "args": {"path": "D:/x.txt"}, "result": '{"bytes_written": 12}',
                     "session_id": "s1", "status": "ok"})
check("成功写操作原样放行", ok is None)

ro = P.bound_result({"tool_name": "read_file", "args": {"path": "D:/x.txt"}, "result": "permission denied",
                     "session_id": "s1", "status": "error"})
check("只读错误原样放行", ro is None)

term_ok = P.bound_result({"tool_name": "terminal", "args": {"command": "grep x"}, "result": '{"output": "no match", "exit_code": 1}',
                          "session_id": "s1", "status": "ok"})
check("terminal 普通失败不标注", term_ok is None)
term_to = P.bound_result({"tool_name": "terminal", "args": {"command": "sleep 999"}, "result": "Command timed out after 300s",
                          "session_id": "s1", "status": "error"})
check("terminal 超时标注 unknown", isinstance(term_to, str) and "unknown" in term_to)

# 关掉开关后必须完全不标注
P._config = lambda: {**B.DEFAULTS, "effect_state": False}
off = P.bound_result({"tool_name": "patch", "args": {"path": "D:/x.txt"}, "result": patch_err,
                      "session_id": "s1", "status": "error"})
check("effect_state=false 时不标注", off is None)

# 汇总行
summary = B.summarize([{"mode": "effect", "state": "none", "tool_name": "patch", "saved_chars": 0},
                       {"mode": "effect", "state": "unknown", "tool_name": "terminal", "saved_chars": 0},
                       {"mode": "bounded", "tool_name": "read_file", "saved_chars": 5000}], title="测试")
check("汇总含效果判定行", "效果判定 2 次" in summary and "未知 1" in summary, [l for l in summary.splitlines() if "效果" in l][:1])
check("汇总节省不被 effect 行干扰", "5.0k" in summary, [l for l in summary.splitlines() if "省下" in l])

# /ops 真跑一遍（走真实账本 + 桩目录），effect 行不能让命令崩
ops_out = P._handle_ops("")
check("/ops 能读出效果判定行", isinstance(ops_out, str) and "效果判定" in ops_out,
      [l for l in (ops_out or "").splitlines() if "效果" in l][:1])

print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败：" + "；".join(FAILS))
    sys.exit(1)
print("=== 全部通过")
