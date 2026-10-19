# -*- coding: utf-8 -*-
"""账本跨进程追加：8 个进程 × 40 行，一行都不能丢。

用法: python unit_ledger_lock.py
线程由进程内锁串行化（stress_effect.py 覆盖），这里只测进程之间：
桌面后端、a2a 会话、cron 会同时往同一个 savings.jsonl 追加。
"""
import importlib.util
import json
import multiprocessing as mp
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
if not (ROOT / "bounding.py").exists():  # 在插件安装目录里跑，而不是仓库根
    ROOT = pathlib.Path.home() / "AppData/Local/hermes/plugins/ops-lite"
BOUNDING = ROOT / "bounding.py"
WORKERS, PER_WORKER = 8, 40


def _load():
    spec = importlib.util.spec_from_file_location("bl_lock", BOUNDING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worker(args):
    ledger, tag = args
    bl = _load()
    for index in range(PER_WORKER):
        bl.append_ledger(ledger, {"ts": 0, "session_id": tag, "tool_name": "probe",
                                  "mode": "effect", "state": "none", "seq": index,
                                  "saved_chars": 0})


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    ledger = tmp / "savings.jsonl"
    fails = []

    ctx = mp.get_context("spawn")
    with ctx.Pool(WORKERS) as pool:
        pool.map(_worker, [(ledger, f"p{i}") for i in range(WORKERS)])

    raw = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    lines = [line for line in raw.splitlines() if line.strip()]
    expected = WORKERS * PER_WORKER
    bad = 0
    seen = set()
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            bad += 1
            continue
        seen.add((row.get("session_id"), row.get("seq")))

    def check(name, cond, extra=""):
        print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    check(f"{WORKERS} 进程 × {PER_WORKER} 行全部落盘", len(lines) == expected, f"{len(lines)}/{expected}")
    check("没有损坏行", bad == 0, f"{bad} 行")
    check("没有重复或覆盖", len(seen) == expected, f"{len(seen)}/{expected}")
    lock_file = tmp / "savings.jsonl.lock"
    check("锁文件存在且不累积内容", lock_file.exists() and lock_file.stat().st_size <= 1,
          f"{lock_file.stat().st_size if lock_file.exists() else 'missing'} B")

    print()
    if fails:
        print("=== " + str(len(fails)) + " 项失败：" + "；".join(fails))
        return 1
    print("=== 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
