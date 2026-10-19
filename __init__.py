"""ops-lite: bound tool results before they enter context.

Ported from dsh-ops (T-Auto/dsh-ops) to the Hermes seam that exists here:
``transform_tool_result`` runs after ``post_tool_call`` and before the result is
appended to conversation context, so this plugin can keep the useful edges of a
tool result, spill the full text to disk, and short-circuit an unchanged repeat.

Three mechanisms, matching dsh-ops' file/PTC components:
  * bounded results   — head/tail kept, middle dropped but retrievable by path
  * repeat short-circuit — same args + same content => "unchanged", no re-echo
  * error budget      — failures keep a larger window (errors carry the real signal)

Plus a prompt-only ladder (``ops-lite-ladder``) telling the model how to retrieve and
why batching beats one-tool-per-turn. Like dsh-ops, the ladder never switches tools by
itself — it only steers.

Observer discipline: the hook never raises, never blocks, never rewrites arguments.
Everything is local; no network, no credentials.

Config (``config.yaml`` → ``plugins.entries.ops-lite``), all optional:
  max_chars            8000   bound a result larger than this (chars)
  head_chars           2000   chars of head to keep
  tail_chars           1200   chars of tail to keep
  error_max_chars     24000   larger budget for results the runtime flagged as errors
  repeat_min_chars     2000   above this size, eligible for repeat short-circuit
  repeat_shortcircuit  true   set false to disable short-circuiting entirely
  spill_max_files       300   newest spill files to keep
  spill_ttl_hours       168   drop spill files older than this (0 = never by age)
  effect_state         true   label a failed write with none / applied / unknown
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import bounding

logger = logging.getLogger(__name__)

PLUGIN_NAME = "ops-lite"

_HELP = """\
/ops: what ops-lite saved by bounding tool results

  /ops          last 24 hours
  /ops 7d       any number of hours or days (6h, 30d, all)
  /ops session  the current session only
  /ops files    list the newest spill files with their sizes
  /ops clear    delete the ledger (spill files stay until pruned)
"""

LADDER = """\
## 工具输出的取回阶梯（ops-lite）

超过预算的工具输出会被收敛：普通文本保留「前若干行 + 后若干行」，**JSON 文档（对象/数组）
在文档内部收敛**——键与元素都还在，长的值被截断或省略。全文写在 spill 文件里并附路径与 sha1。
同参数重复读取会返回「结果未变化」。

1. 先只读回显的部分，判断被省略的内容是否真的需要。
2. 需要细节时按行区间取 spill 文件（read_file 带 offset/limit），不要整篇重读。
3. 看到「结果未变化」就复用上一轮已读到的内容，不要换姿势再读一次。
4. 一次能做完的多件独立事放进同一轮（并行工具调用，或用 execute_code 批量串起来）：
   每多一轮，整个会话上下文就要重新付一次进场费，批量是省上下文最直接的一招。
"""

# (tool_name, args_hash) -> (content_hash, spill path, chars, lines)
_REPEAT_MEMORY: dict[tuple[str, str], tuple[str, str, int, int]] = {}
_REPEAT_MEMORY_MAX = 200

# sha1 -> (source path, offset, limit, issued_at): the re-read we offered the model, so a later
# read of that file at that offset can be recorded as a retrieval. Process-local, bounded.
_HINT_MEMORY: dict[str, tuple[str, int, int, float]] = {}
_HINT_MEMORY_MAX = 200
# Tools whose calls can retrieve something we bounded.
_RETRIEVAL_TOOLS = frozenset({"read_file", "search_files"})


def _cfg_flag(name: str, default: bool = True) -> bool:
    try:
        return bool(_config().get(name, default))
    except Exception:
        return default


def _remember_hint(digest: str, hint: Any, now: float) -> None:
    """Remember where we told the model to look, so an actual look can be counted."""
    if not isinstance(hint, str) or not digest:
        return
    m = re.search(r'path="([^"]+)"(?:, offset=(\d+))?(?:, limit=(\d+))?', hint)
    if not m:
        return
    if len(_HINT_MEMORY) >= _HINT_MEMORY_MAX:
        _HINT_MEMORY.pop(next(iter(_HINT_MEMORY)), None)
    _HINT_MEMORY[digest] = (m.group(1), int(m.group(2) or 1), int(m.group(3) or 200), now)


def _note_retrieval(tool_name: str, args: Any, session_id: str, now: float) -> None:
    """Append a ledger row when this call actually reads back something we bounded.

    Two exact signals, one cheap test each on the hot path: the path is inside our spill dir
    (filename starts with the sha1), or it is a source file we pointed at, read at the offset
    we suggested. Append-only, so no row is ever rewritten (concurrency-safe by construction).
    """
    if tool_name not in _RETRIEVAL_TOOLS or not isinstance(args, dict):
        return
    path = bounding._path_arg(args)
    if not path:
        return
    sha, via = "", None
    if path.startswith(str(_spill_dir())):
        name = Path(path).name
        sha = name.split(".", 1)[0]
        via = "sidecar" if name.endswith(".field.txt") else "spill"
    else:
        for known_sha, (hint_path, offset, limit, issued) in list(_HINT_MEMORY.items()):
            if hint_path != path or issued > now:
                continue
            try:
                first_line = int(args.get("offset") or 1)
            except Exception:
                first_line = 1
            if offset <= first_line <= offset + max(limit, 200) + 200:
                sha, via = known_sha, "source"
                break
    if not via or not sha:
        return
    _record({"ts": now, "session_id": session_id, "tool_name": tool_name, "mode": "retrieve",
             "via": via, "sha1": sha, "original_chars": 0, "returned_chars": 0, "saved_chars": 0})

_config_cache: tuple[float, dict[str, Any]] = (0.0, {})
_CONFIG_TTL = 5.0
_call_counter = 0
_PRUNE_EVERY = 50


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _spill_dir() -> Path:
    return _data_dir() / bounding.SPILL_DIRNAME


def _ledger_path() -> Path:
    return _data_dir() / bounding.LEDGER_FILENAME


def _config() -> dict[str, Any]:
    """Effective config, re-read at most every few seconds; never raises."""
    global _config_cache
    now = time.monotonic()
    cached_at, cached = _config_cache
    if cached and now - cached_at < _CONFIG_TTL:
        return cached
    raw: Any = None
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        raw = entries.get(PLUGIN_NAME) if isinstance(entries, dict) else None
    except Exception as exc:  # config unreadable -> defaults, never break the tool path
        logger.debug("ops-lite: config unavailable (%s), using defaults", exc)
    cfg = bounding.resolve_config(raw)
    _config_cache = (now, cfg)
    return cfg


def _record(row: dict[str, Any]) -> None:
    try:
        bounding.append_ledger(_ledger_path(), row)
    except Exception as exc:
        logger.debug("ops-lite: could not append ledger: %s", exc)


def _maybe_prune(cfg: dict[str, Any]) -> None:
    global _call_counter
    _call_counter += 1
    if _call_counter % _PRUNE_EVERY:
        return
    try:
        bounding.prune_spill(_spill_dir(), max_files=int(cfg["spill_max_files"]),
                             ttl_hours=float(cfg["spill_ttl_hours"]))
    except Exception as exc:
        logger.debug("ops-lite: spill prune failed: %s", exc)


def _remember(key: tuple[str, str], digest: str, spill: Path, chars: int, lines: int) -> None:
    if len(_REPEAT_MEMORY) >= _REPEAT_MEMORY_MAX:
        _REPEAT_MEMORY.pop(next(iter(_REPEAT_MEMORY)), None)
    _REPEAT_MEMORY[key] = (digest, str(spill), chars, lines)


def bound_result(payload: dict[str, Any]) -> Optional[str]:
    """Return a replacement string for this tool result, or None to leave it alone.

    Kept import-free of Hermes state so it can be exercised directly in tests.
    """
    tool_name = str(payload.get("tool_name") or "")
    args = payload.get("args")
    session_id = str(payload.get("session_id") or "")
    if _cfg_flag("retrieve_log"):
        try:
            _note_retrieval(tool_name, args, session_id, time.time())
        except Exception as exc:
            logger.debug("ops-lite: retrieval note failed: %s", exc)
    result = payload.get("result")
    if not isinstance(result, str):
        return None

    cfg = _config()
    is_error = bool(payload.get("error_type")) or str(payload.get("status") or "").lower() in {"error", "failed"}
    # Effect label first, before the SKIP_TOOLS return: write_file and patch are skipped by the
    # bounding path (their receipts are contracts) but a *failed* one is exactly what needs the
    # label. dsh-ops carries effectState on every failure; this is the seam that can host it.
    effect: Optional[str] = None
    evidence: Optional[str] = None
    if is_error and bool(cfg.get("effect_state", True)):
        try:
            effect, evidence = bounding.classify_effect_detail(
                tool_name, result, error_message=str(payload.get("error_message") or ""), is_error=True)
        except Exception as exc:
            logger.debug("ops-lite: effect classification failed: %s", exc)
    if effect:
        try:
            result = bounding.annotate_effect(result, effect)
        except Exception as exc:
            logger.debug("ops-lite: effect annotation failed: %s", exc)
            effect = None
        else:
            _record({"ts": time.time(), "session_id": session_id, "tool_name": tool_name,
                     "mode": "effect", "state": effect, "evidence": (evidence or "")[:120],
                     "original_chars": 0, "returned_chars": 0, "saved_chars": 0})
    if tool_name in bounding.SKIP_TOOLS:
        return result if effect else None

    size = len(result)
    budget = bounding.budget_for(cfg, tool_name, is_error=is_error)

    key = (tool_name, bounding.args_hash(payload.get("args")))
    now = time.time()
    digest = bounding.content_hash(result)
    envelope = bounding.unwrap_envelope(result)
    total_lines = (len(bounding.split_lines(envelope[0][envelope[1]])) if envelope
                   else len(bounding.split_lines(result)))
    # Spill only when this result could actually be handed back in bounded form. Writing every
    # tool result to disk costs I/O, leaves unreviewed output lying around, and grows the spill
    # directory for results nobody will ever retrieve (independent review, 2026-10-16).
    wants_repeat = (bool(cfg["repeat_shortcircuit"]) and tool_name in bounding.REPEAT_SAFE_TOOLS
                    and size >= int(cfg["repeat_min_chars"]))
    spill: Optional[Path] = None
    if size > budget or wants_repeat:
        try:
            spill = bounding.write_spill(_spill_dir(), result, digest)
        except Exception as exc:
            logger.debug("ops-lite: spill write failed (%s); result stays untouched", exc)
            spill = None
    _maybe_prune(cfg)

    # Repeat short-circuit: same tool + same args + same content inside this process.
    repeat_eligible = wants_repeat and spill is not None
    if repeat_eligible:
        previous = _REPEAT_MEMORY.get(key)
        if previous and previous[0] == digest:
            marker = bounding.repeat_marker(tool_name, Path(previous[1]), digest,
                                            chars=size, total=previous[3])
            _record({"ts": now, "session_id": session_id, "tool_name": tool_name, "mode": "repeat",
                     "original_chars": size, "returned_chars": len(marker),
                     "saved_chars": max(0, size - len(marker)), "sha1": digest, "spill": previous[1]})
            return marker
    if size <= budget:
        if repeat_eligible:
            _remember(key, digest, spill, size, total_lines)
        return result if effect else None

    if spill is None:
        return result if effect else None

    marker, meta = bounding.build_replacement(tool_name, result, payload.get("args"), spill, digest,
                                              cfg=cfg, budget=budget, is_error=is_error)
    # Never hand back something longer than what it replaces, and never claim a
    # saving that did not happen (a decoded payload can already fit the budget).
    if marker == result or len(marker) >= size:
        return result if effect else None
    if repeat_eligible:
        _remember(key, digest, spill, size, int(meta.get("total_lines") or total_lines))
    _maybe_prune(cfg)
    _record({"ts": now, "session_id": session_id, "tool_name": tool_name, "mode": "bounded",
             "original_chars": size, "returned_chars": len(marker), "saved_chars": max(0, size - len(marker)),
             "sha1": digest, "spill": str(spill), "structure": meta.get("structure"),
             "lines_total": meta.get("total_lines"), "omitted_lines": meta.get("omitted_lines"),
             "retrieve": meta.get("retrieve"), "error": is_error})
    _remember_hint(digest, meta.get("retrieve"), now)
    return marker


def _on_transform_tool_result(**payload: Any) -> Optional[str]:
    try:
        return bound_result(payload)
    except Exception as exc:  # fail-open: a broken bound must never break a tool call
        logger.debug("ops-lite: transform failed, passing result through: %s", exc)
        return None


def _handle_ops(raw_args: str = "") -> Optional[str]:
    words = (raw_args or "").lower().split()
    known = {"session", "files", "clear", "all", "help", "-h", "--help"}
    spans = [w for w in words if w not in known]
    if "help" in words or "-h" in words or "--help" in words:
        return _HELP
    path = _ledger_path()
    if "files" in words:
        spill = _spill_dir()
        if not spill.exists():
            return "ops-lite: 还没有 spill 文件。"
        entries = sorted(((p.stat().st_mtime, p) for p in spill.glob("*.txt")), reverse=True)[:15]
        lines = [f"ops-lite · spill 文件（最新 {len(entries)} 个，目录 {spill}）"]
        for mtime, file in entries:
            lines.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(mtime))}  {file.stat().st_size:>9,} B  {file.name}")
        return "\n".join(lines)
    if "clear" in words:
        try:
            path.unlink(missing_ok=True)
            return "ops-lite: 账本已清空（spill 文件保留，按 TTL/数量自动回收）。"
        except OSError as exc:
            return f"ops-lite: 清空失败 {exc}"
    now = time.time()
    session_only = "session" in words
    window = 24 * 3600.0
    title = "近 24 小时"
    if "all" in words:
        window, title = 0.0, "全部"
    elif spans:
        unit, value = spans[0][-1], spans[0][:-1]
        try:
            amount = float(value)
        except ValueError:
            return f"Unknown option: {spans[0]}\n\n{_HELP}"
        if unit == "h":
            window, title = amount * 3600.0, f"近 {amount:g} 小时"
        elif unit == "d":
            window, title = amount * 86400.0, f"近 {amount:g} 天"
        else:
            return f"Unknown option: {spans[0]}\n\n{_HELP}"
    rows = bounding.read_ledger(path, since=None if window == 0 else now - window)
    if session_only:
        latest = rows[-1].get("session_id") if rows else None
        rows = [r for r in rows if r.get("session_id") == latest]
        title = f"当前会话（{latest or '无'}）"
    out = bounding.summarize(rows, title=title)
    bounded = {r.get("sha1") for r in rows if r.get("mode") == "bounded" and r.get("sha1")}
    if bounded:
        hits = {r.get("sha1") for r in rows if r.get("mode") == "retrieve"}
        got = len(bounded & hits)
        if got:
            out += (f"\n  取回率：{got}/{len(bounded)} 次（{100 * got / len(bounded):.0f}%）"
                    f"——收敛之后真的有人回头读了留档/专档/原文件"
                    f"（自动统计自 2026-10-19 起，更早的数据未记；人工反查那批约 19%）")
        else:
            out += (f"\n  取回率：{len(bounded)} 次收敛，暂无自动取回记录"
                    f"（自动统计自 2026-10-19 起；此前人工反查约 19%，不是 0）")
    return out


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_system_prompt_section("ops-lite-ladder", LADDER, max_chars=1200)
    ctx.register_command(
        "ops",
        handler=_handle_ops,
        args_hint="[session|7d|all|files] | clear",
        description="Show what ops-lite saved by bounding tool results.",
    )
