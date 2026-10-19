# -*- coding: utf-8 -*-
"""ops-lite 单元断言集（确定性用例，配合 fuzz_rounds.py 的属性测试）。

用法: python unit_ops_lite.py [bounding.py路径]
覆盖：预算与按工具上限、信封/数组/嵌套文档、BOM、保留键碰撞、取回提示与字段专档、
配置校验（含深拷贝）、留档字节保真、自保不变量。
"""
import importlib.util, json, pathlib, re, sys, tempfile

B = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent / "bounding.py")
spec = importlib.util.spec_from_file_location("bl", B); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

TMP = pathlib.Path(tempfile.mkdtemp())
FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def cfg(**over):
    return m.resolve_config(over or None)


def run(text, *, tool="tool_call", args=None, budget=12000, c=None, is_error=False):
    c = c or cfg()
    d = m.content_hash(text); sp = m.write_spill(TMP, text, d)
    return m.build_replacement(tool, text, args or {"path": "D:/x"}, sp, d, cfg=c, budget=budget, is_error=is_error)


def hint_parts(hint):
    mm = re.search(r'path="([^"]+)"(?:, offset=(\d+))?', hint or "")
    return (mm.group(1), int(mm.group(2) or 0)) if mm else (None, None)


# ---- 1. 预算与按工具上限 ----
c = cfg()
check("覆盖表默认值", m.budget_for(c, "skill_view", is_error=False) == 6000
      and m.budget_for(c, "skills_list", is_error=False) == 4000
      and m.budget_for(c, "read_file", is_error=False) == c["max_chars"])
check("错误输出用错误预算", m.budget_for(c, "skill_view", is_error=True) == c["error_max_chars"])
c2 = m.resolve_config({"tool_budget_overrides": {"skill_view": "6000", "read_file": -5, 7: 100, "terminal": 9000}})
check("非法覆盖被过滤", c2["tool_budget_overrides"] == {"terminal": 9000})
c3, c4 = cfg(), cfg(); c3["tool_budget_overrides"]["skill_view"] = 999
check("配置深拷贝（不共享嵌套 dict）", c4["tool_budget_overrides"]["skill_view"] == 6000)
check("ttl=0 合法 / max_files=0 拒绝",
      m.resolve_config({"spill_ttl_hours": 0})["spill_ttl_hours"] == 0
      and m.resolve_config({"spill_max_files": 0})["spill_max_files"] == m.DEFAULTS["spill_max_files"])

mid = "\n".join(f"item{i}|{'d' * 60}" for i in range(70))          # ~4860 字符
check("skills_list 4860 字符按 4000 收敛", len(run(mid, tool="skills_list", budget=4000)[0]) < len(mid))
check("同内容 read_file 按 8000 不动", run(mid, tool="read_file", budget=8000)[0] == mid)
check("未超预算原样放行不谎报", run("z" * 263, budget=8000)[0] == "z" * 263)

# ---- 2. 形态覆盖 ----
lines = "".join(f"{i}|{'x' * 30}\n" for i in range(1, 500))
env = json.dumps({"content": lines, "total_lines": 499}, ensure_ascii=False)
out, meta = run(env, tool="read_file")
check("信封按行收敛", meta["structure"] == "json:content" and json.loads(out)["content"].startswith("1|"))
arr = json.dumps([{"i": i, "t": "A" * 80} for i in range(400)], ensure_ascii=False)
out, meta = run(arr)
check("紧凑数组走文档分支可解析且元素不丢",
      meta["structure"] == "json:document" and len(json.loads(out)["items"]) == 400, f"{len(arr):,}→{len(out):,}")
deep = json.dumps({"result": json.dumps({"content": "Z" * 9000})}, ensure_ascii=False)
out, _ = run(deep)
check("嵌套 JSON 字段每层可解析", json.loads(json.loads(out)["result"])["content"].startswith("Z"))
bom = "\ufeff" + json.dumps({"content": "b" * 20000}, ensure_ascii=False)
check("BOM 信封可收敛", len(run(bom, tool="read_file")[0]) < len(bom), f"{len(bom):,}→{len(run(bom, tool='read_file')[0]):,}")
check("BOM 且未超预算：字节保真放行", run("\ufeff" + json.dumps({"content": "b" * 2000}), budget=8000)[0].startswith("\ufeff"))
out, _ = run(json.dumps({"_ops_lite": {"note": "假的"}, **{f"k{i}": "V" * 300 for i in range(200)}}, ensure_ascii=False))
ml = json.loads(out)["_ops_lite"]
check("保留键碰撞：元数据仍生效", isinstance(ml, dict) and pathlib.Path(ml["full_text"]).exists())
out, _ = run("y" * 40000)
check("超长单行字符切且不变长", len(out) < 5000)

# ---- 3. 取回提示 / 字段专档 ----
srcfile = TMP / "src.txt"
srcfile.write_text("\n".join(f"L{i}-{'x' * 30}" for i in range(1, 2001)), encoding="utf-8")
content = "\n".join(f"L{i}-{'x' * 30}" for i in range(500, 1500))
out, meta = run(json.dumps({"content": content, "total_lines": 2000, "path": str(srcfile)}, ensure_ascii=False),
                tool="read_file", args={"path": str(srcfile), "offset": 500, "limit": 1000})
p, off = hint_parts(meta["retrieve"])
rec = pathlib.Path(p).read_text(encoding="utf-8").splitlines()[off - 1]
check("源文件提示 offset 叠加原起始行", p == str(srcfile) and rec.startswith(f"L{500 + meta['head_lines']}-"), f"offset={off} 首行={rec[:8]!r}")
term = "\n".join(f"log{i}: {'y' * 40}" for i in range(1, 1501))
out, meta = run(json.dumps({"output": term, "exit_code": 0}, ensure_ascii=False), tool="terminal", args={"command": "x"})
p2, off2 = hint_parts(meta["retrieve"])
side = pathlib.Path(p2)
check("信封类结果给字段专档且内容逐字节一致",
      side.name.endswith(".field.txt") and side.read_text(encoding="utf-8") == term, side.name)
rec2 = side.read_text(encoding="utf-8").splitlines()[off2 - 1]
check("专档 offset 对应被省略的首行", rec2 == term.splitlines()[meta["head_lines"]], f"{rec2[:12]!r}")

# ---- 4. 留档与自保 ----
t = "a\r\nb\nc\rd"; d = m.content_hash(t); sp = m.write_spill(TMP, t, d)
check("留档逐字节保真（CRLF 不改写）", sp.read_bytes() == t.encode("utf-8"))
lone = json.dumps({"content": "\ud800" * 2000}, ensure_ascii=False)
sp2 = m.write_spill(TMP, lone, m.content_hash(lone))
check("孤立代理对可写出且无 .part 残留", sp2.exists() and not list(TMP.glob("*.part")))
bad_arr = json.dumps([{"i": i, "t": "Q" * 80} for i in range(400)], ensure_ascii=False)
out, _ = run(bad_arr, budget=8000)
check("任何路径都不得变长（含文档收敛）", len(out) <= len(bad_arr))
print(f"\n=== {len(FAILS)} 项失败" if FAILS else "\n=== 全部通过")
sys.exit(1 if FAILS else 0)
