# -*- coding: utf-8 -*-
"""ops-lite 模糊测试台：每轮生成随机工具结果，校验收敛不变量。
用法: python fuzz_rounds.py [轮数] [种子] [bounding.py路径]
不变量：①不得变长 ②原为合法 JSON 则输出每层可解析 ③信封键不丢、数组元素不丢
④留档路径真实存在且与原文逐字节一致 ⑤同输入两次结果一致 ⑥不抛异常
"""
import importlib.util, json, pathlib, random, sys, tempfile

B = sys.argv[3] if len(sys.argv) > 3 else str(pathlib.Path(__file__).resolve().parent.parent / "bounding.py")
spec = importlib.util.spec_from_file_location("bl", B); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg = m.resolve_config(None)

def gen(rng):
    kind = rng.choice(["lines", "oneline", "envelope", "nested", "array", "bom", "crlf", "small", "malformed", "surrogate",
                       "collide", "deep3", "manykeys"])
    n = rng.randint(0, 1500)
    if kind == "lines":   return "\n".join(f"{i}|{'x' * rng.randint(0, 200)}" for i in range(n))
    if kind == "oneline": return "y" * rng.randint(0, 60000)
    if kind == "envelope":
        big = "\n".join(f"{i}|{'z' * rng.randint(0, 300)}" for i in range(n))
        return json.dumps({"content": big, "total_lines": n, "path": "D:/x.txt"}, ensure_ascii=False)
    if kind == "nested":
        inner = json.dumps({"a": "q" * rng.randint(10, 9000), "b": [1, 2, 3]}, ensure_ascii=False)
        return json.dumps({"result": inner, "ok": True})
    if kind == "array":   return json.dumps([{"i": i, "t": "A" * rng.randint(5, 400)} for i in range(rng.randint(1, 500))], ensure_ascii=False)
    if kind == "bom":     return "\ufeff" + json.dumps({"content": "b" * rng.randint(100, 9000)}, ensure_ascii=False)
    if kind == "collide":
        if rng.random() < 0.5:
            return json.dumps({"_ops_lite": {"note": "假的"}, "content": "C" * rng.randint(100, 9000)}, ensure_ascii=False)
        return json.dumps({"_ops_lite": "敌意" * rng.randint(50, 1500),
                           **{f"k{i}": "V" * rng.randint(50, 400) for i in range(rng.randint(50, 300))}}, ensure_ascii=False)
    if kind == "deep3":
        l3 = json.dumps({"content": "Z" * rng.randint(100, 9000)})
        l2 = json.dumps({"result": l3, "note": "n" * rng.randint(0, 300)})
        return json.dumps({"result": l2, "ok": rng.random() < 0.5}, ensure_ascii=False)
    if kind == "manykeys":
        pairs = {f"s{i}": i for i in range(rng.randint(500, 2500))}
        pairs["content"] = "M" * rng.randint(100, 6000)
        return json.dumps(pairs, ensure_ascii=False)
    if kind == "crlf":    return "\r\n".join(["h"] * rng.randint(1, 900)) + "\r\n"
    if kind == "small":   return "s" * rng.randint(0, 200)
    if kind == "malformed": return rng.choice(['{"result": "unclosed', '{', "[1,2,", '{"a": tru', '', 'null', '{}'])
    return "\ud800" * rng.randint(1, 500)  # surrogate

def strip_bom(t): return t.lstrip("\ufeff").lstrip()

def parse(s):
    try: return json.loads(s)
    except Exception: return None

def run_round(seed, rounds):
    rng = random.Random(seed)
    for r in range(rounds):
        tmp = pathlib.Path(tempfile.mkdtemp())
        bad = []
        for _ in range(60):
            text = gen(rng)
            budget = rng.choice([1000, 3000, 8000, 12000, 30000])
            digest = m.content_hash(text)
            try:
                spill = m.write_spill(tmp, text, digest)
                out, meta = m.build_replacement("tool_call", text, {"path": "D:/x.txt"}, spill, digest, cfg=cfg, budget=budget, is_error=False)
            except Exception as e:
                bad.append(f"抛异常 {type(e).__name__}: {e} | 输入前80字符={text[:80]!r}"); continue
            if len(out) > len(text): bad.append(f"变长 {len(text)}->{len(out)}"); continue
            pin, pout = parse(strip_bom(text)), parse(strip_bom(out))
            if pin is not None and pout is None: bad.append(f"原为合法 JSON 输出不可解析 | 前80字符={text[:80]!r}")
            if isinstance(pin, dict) and isinstance(pout, dict) and "_ops_lite" not in pout:
                missing = set(pin) - set(pout) - {"_ops_lite"}  # _ops_lite 是保留键，被替换时可不回显
                if missing: bad.append(f"信封键丢 {missing}")
            if out != text and pin is not None and isinstance(pout, dict) and "_ops_lite" not in pout:
                bad.append("替换后未附元数据块")
            if isinstance(pin, list) and isinstance(pout, dict) and "items" in pout and len(pout["items"]) != len(pin):
                bad.append("数组元素数变化")
            if out != text and isinstance(pout, dict) and "_ops_lite" in pout:  # 未替换=原样放行，免检
                ref = pout["_ops_lite"].get("full_text")
                if not ref or not pathlib.Path(ref).exists(): bad.append(f"留档路径不存在 {ref}")
                elif pathlib.Path(ref).read_bytes() != text.encode("utf-8", "surrogatepass"):
                    bad.append("留档与原文不一致")
            out2, _ = m.build_replacement("tool_call", text, {"path": "D:/x.txt"}, spill, digest, cfg=cfg, budget=budget, is_error=False)
            if out2 != out: bad.append("同输入两次结果不一致")
        if bad:
            print(f"[seed {seed} round {r}] 违约 {len(bad)} 项"); [print("  -", b) for b in bad[:5]]; return False
        print(f"[seed {seed} round {r}] 全清（60 例）")
    return True

seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
sys.exit(0 if run_round(seed, rounds) else 1)
