"""Pure helpers for ops-lite: envelope-aware bounding, spill files, savings ledger.

No Hermes imports on purpose: everything is testable standalone, and the hook path
only ever calls these functions.

Why envelope-aware: most large Hermes tool results are JSON whose payload sits in a
string field with newlines escaped (``read_file`` -> ``content``, ``terminal`` ->
``output``, ``search_files`` -> ``matches_text``). Splitting such a result by lines
sees exactly one line, so a naive bounding step would save nothing at all. This module
decodes the big field, bounds *that* by lines, and rebuilds the envelope, so the model
keeps the shape it already knows how to read.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable

DEFAULTS: dict[str, Any] = {
    "max_chars": 8000,
    "head_chars": 2000,
    "tail_chars": 1200,
    "error_max_chars": 24000,
    "repeat_min_chars": 2000,
    "repeat_shortcircuit": True,
    "json_value_chars": 2000,
    "spill_max_files": 300,
    "spill_ttl_hours": 168,
    # Per-tool character budgets for read-and-summarise tools. Evidence (2026-10-19): overall
    # retrieval rate is 19%, and skill_view results were read back only 1 time out of 14, so
    # these tolerate a tighter cut than data-returning tools.
    "tool_budget_overrides": {"skill_view": 6000, "skills_list": 4000, "session_search": 6000},
    "retrieve_log": True,
    # Tag a failed write/command with what actually happened on disk (dsh-ops effectState).
    "effect_state": True,
}

LEDGER_FILENAME = "savings.jsonl"
SPILL_DIRNAME = "spill"

# A JSON string field at least this big is treated as the result payload.
FIELD_MIN_CHARS = 1500

# Per-value trimming floor and the compact marker used inside JSON documents (repeated per
# value, so it stays short; the sha1 and the spill path live in the top-level _ops_lite block).
_VALUE_FLOOR = 120
# The drop phase blanks the largest surviving values; below this size blanking stops paying
# for itself. Must stay under the trimmed size (``_VALUE_FLOOR`` + marker) or it can never fire.
_DROP_FLOOR = 100
_MARKER = "…[ops-lite 省略 {n:,} 字符]…"

# Tools whose identical-argument results are stable enough inside one session to
# short-circuit. File/blob reads only: web, search and vision results can legitimately
# change between two identical calls.
REPEAT_SAFE_TOOLS = frozenset({
    "read_file",
    "search_files",
    "skill_view",
    "skills_list",
    "tool_describe",
})

# Never touched: results the runtime or the user reads as a contract, and results
# that are already short by construction.
SKIP_TOOLS = frozenset({
    "write_file",
    "patch",
    "todo_list",
    "clarify",
    "kanban_show",
})

# ---- effect state (ported from dsh-ops' Failure.effectState) -------------------------------
#
# A failed write leaves the model with one question it must not get wrong: did anything
# happen? The answer decides whether a retry is safe, and the tool's own wording usually
# contains it — ``write_file`` says "The file was NOT modified", ``patch`` says it could not
# find a match. This classifies that wording into dsh-ops' three states and hands back a
# one-line instruction. It is a label on the result, never an action: nothing here retries,
# re-reads or touches the filesystem.
EFFECT_TOOLS = frozenset({"write_file", "patch", "skill_manage"})
EFFECT_SHELL_TOOLS = frozenset({"terminal", "execute_code"})

# Order matters: the applied phrases are the specific ones ("was modified" never appears
# inside "was NOT modified"), and a no-effect phrase wins over a generic danger token.
_EFFECT_APPLIED = (
    "was modified", "has been modified", "was written", "has been written", "has been created",
    "written but", "saved but", "created but", "applied but", "partially applied", "partially written",
)
_EFFECT_NONE = (
    "not modified", "no changes made", "nothing was written", "refusing to", "refused",
    "could not find a match", "old_string not found", "not found in the file", "has not been read",
    "no such file", "does not exist", "permission denied", "access is denied", "read-only",
    "invalid argument", "missing required", "not allowed", "outside the workspace", "escapes",
    "blocked by",
)
_EFFECT_UNKNOWN = (
    "timed out", "timeout", "interrupted", "killed", "terminated", "signal", "connection reset",
    "broken pipe", "aborted", "unknown effect", "effectstate",
)
EFFECT_NEXT = {
    "none": "未发生写入；修正请求后可安全重试",
    "applied": "已产生变更；不要重试，先核验当前状态",
    "unknown": "效果未知；先核验目标再决定是否重试",
}
# Windows emulates O_APPEND per handle: two threads can seek to the same end offset and one line
# overwrites the other (measured: 480 appends → 344 lines). The gateway runs tools in threads, so
# serialize same-process appends; cross-process behaviour is unchanged.
_LEDGER_LOCK = threading.Lock()



def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def args_hash(args: Any) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = repr(args)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def resolve_config(raw: Any) -> dict[str, Any]:
    """Merge a config mapping over DEFAULTS, ignoring unknown keys and bad types.

    ``spill_ttl_hours`` accepts 0 ("never by age", as the README promises); the size knobs
    require a positive value, and 0 for ``spill_max_files`` is rejected because it would
    mean "keep nothing".
    """
    # Deep-copy: a shared nested dict would let one resolved config mutate another's defaults.
    cfg = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DEFAULTS.items()}
    if isinstance(raw, dict):
        for key, default in DEFAULTS.items():
            value = raw.get(key)
            if isinstance(default, bool):
                if isinstance(value, bool):
                    cfg[key] = value
            elif isinstance(default, dict):
                if isinstance(value, dict):
                    cfg[key] = {str(name): amount for name, amount in value.items()
                                if isinstance(name, str) and isinstance(amount, int)
                                and not isinstance(amount, bool) and amount > 0}
            elif isinstance(default, int):
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
                if value > 0 or (value == 0 and key == "spill_ttl_hours"):
                    cfg[key] = value
    return cfg


def budget_for(cfg: dict[str, Any], tool_name: str, *, is_error: bool) -> int:
    """Effective budget for one result: per-tool override, else the global knob.

    Error text keeps the error budget unconditionally — a failure's wording is what you
    diagnose from, and its tool name says nothing about how much of it matters.
    """
    if is_error:
        return int(cfg["error_max_chars"])
    override = (cfg.get("tool_budget_overrides") or {}).get(tool_name)
    return int(override or cfg["max_chars"])


def split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def count_lines(text: str) -> int:
    return len(split_lines(text))


def unwrap_envelope(text: str) -> Optional[tuple[dict[str, Any], str]]:
    """If *text* is a JSON object with one big string field, return (obj, field_key).

    "Big" is ``FIELD_MIN_CHARS``; the largest such field wins. A leading BOM is tolerated:
    some Windows tools and file reads emit one, and json.loads refuses it.
    """
    probe = text.lstrip("\ufeff").lstrip()
    if not probe.startswith("{"):
        return None
    try:
        obj = json.loads(probe)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    best_key, best_len = None, 0
    for key, value in obj.items():
        if isinstance(value, str) and len(value) > best_len:
            best_key, best_len = key, len(value)
    if best_key is None or best_len < FIELD_MIN_CHARS:
        return None
    return obj, best_key


def bound_lines(text: str, *, head_chars: int, tail_chars: int) -> tuple[str, str, int, int, int]:
    """Return (head, tail, head_lines, tail_lines, total_lines) cut on line boundaries.

    ``head`` takes whole lines up to ``head_chars``; ``tail`` takes whole lines back from
    the end up to ``tail_chars``, never overlapping the head.
    """
    lines = split_lines(text)
    total = len(lines)
    head_parts: list[str] = []
    used = 0
    for line in lines:
        if head_parts and used + len(line) > head_chars:
            break
        head_parts.append(line)
        used += len(line)
    head_n = len(head_parts)
    tail_parts: list[str] = []
    used = 0
    for line in reversed(lines[head_n:]):
        if tail_parts and used + len(line) > tail_chars:
            break
        tail_parts.insert(0, line)
        used += len(line)
    tail_n = len(tail_parts)
    return "".join(head_parts), "".join(tail_parts), head_n, tail_n, total


def bound_chars(text: str, *, head_chars: int, tail_chars: int) -> tuple[str, str, bool]:
    """Character cut for text with no usable line structure. Returns (head, tail, complete)."""
    if len(text) <= head_chars + tail_chars:
        return text, "", True
    return text[:head_chars], text[-tail_chars:], False


def _path_arg(args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "file", "target"):
        value = args.get(key)
        if isinstance(value, str) and value and not value.startswith(("{", "[")):
            return value
    return None


def _source_offset(args: Any) -> int:
    """Line number the original bounded range started at (read_file offset=...)."""
    if isinstance(args, dict):
        try:
            return max(1, int(args.get("offset") or 1))
        except Exception:
            return 1
    return 1


def retrieval_hint(tool_name: str, args: Any, spill: Path, *, lines_usable: bool,
                   head_lines: int, source_offset: int = 1,
                   sidecar: Optional[Path] = None) -> str:
    """The cheapest next step for the model, given what it just lost.

    ``source_offset`` matters: a read_file(offset=500) result was cut from file line 500, so
    the first omitted line is 500 + head_lines, not head_lines + 1 (review, 2026-10-19).
    ``sidecar`` is a plain-text copy of the bounded field: the spill itself is the escaped
    JSON envelope, whose line numbering has nothing to do with the "省略 N 行" the marker
    reports, so paging the spill by line would hand back the wrong text.
    """
    path = _path_arg(args)
    if tool_name in {"read_file", "skill_view"} and path:
        return f'read_file(path="{path}", offset={source_offset + head_lines}, limit=200)'
    target = sidecar or spill
    if lines_usable:
        return f'read_file(path="{target}", offset={head_lines + 1}, limit=200)'
    return f'search_files(pattern="关键词", path="{target}")'


def write_spill(spill_dir: Path, text: str, digest: str) -> Path:
    """Write the full text once per content hash; returns the path (dedup by hash).

    ``newline=""`` is load-bearing: the default text mode rewrites ``\\n`` as the platform
    line separator on Windows, so the spill would no longer be a byte-faithful copy and
    recovering from it would silently alter the tool's own output.
    """
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / f"{digest}.txt"
    if not path.exists():
        tmp = path.with_suffix(".part")
        try:
            # surrogatepass: a lone surrogate (possible in a tool result carrying broken UTF-16)
            # would otherwise raise mid-write and leave the .part behind forever.
            with tmp.open("w", encoding="utf-8", errors="surrogatepass", newline="") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except Exception:
            with suppress(Exception):
                tmp.unlink()
            raise
    return path


def shrink_json_document(text: str, *, value_budget: int, digest: str, target_chars: int | None = None,
                         max_depth: int = 3, protect: frozenset[str] | None = None, max_drops: int = 400,
                         drop_floor: int | None = None):
    """Bound a JSON *document* by trimming its long string values, keys and shape intact.

    Needed because an envelope field is often itself a JSON document (an MCP result
    wrapping ``{"result": "<json>"}``). Cutting such a field by characters produces a
    string nobody can parse any more. Trimming values keeps every level loadable.

    Starts at ``value_budget`` per value and halves it until the serialized document fits
    ``target_chars`` (or the floor is reached), so a document with many medium values still
    gets bounded instead of staying almost full size.

    Returns (json_text, trimmed_values) or None when there is nothing to do.
    """
    try:
        doc = json.loads(text)
    except Exception:
        return None
    if not isinstance(doc, (dict, list)):
        return None
    protect = protect or frozenset()

    def size() -> int:
        return len(json.dumps(doc, ensure_ascii=False))

    changed = 0
    ladder: list[int] = []
    budget = max(int(value_budget), _VALUE_FLOOR)
    while True:
        ladder.append(budget)
        if budget == _VALUE_FLOOR:
            break
        budget = max(budget // 2, _VALUE_FLOOR)
    for candidate in ladder:
        changed += _shrink_values(doc, value_budget=candidate, digest=digest, depth=0, max_depth=max_depth,
                                  protect=protect)
        if target_chars and size() <= target_chars:
            break
    # Still over budget: trim alone cannot get a document with hundreds of medium values under
    # the target. Blank the largest surviving values one at a time (keys and shape kept, the
    # protected recovery block untouched) until it fits or nothing meaningful is left to blank.
    if target_chars and size() > target_chars:
        changed += _blank_largest(doc, target=target_chars, size_fn=size, digest=digest,
                                  protect=protect, max_drops=max_drops,
                                  floor=_DROP_FLOOR if drop_floor is None else drop_floor)
    if not changed:
        return None
    return json.dumps(doc, ensure_ascii=False), changed


def _blank_largest(node: Any, *, target: int, size_fn, digest: str, protect: frozenset[str],
                   max_drops: int, floor: int) -> int:
    """Blank the largest string values until *size_fn* fits *target*. Never wins on size alone:
    a value below ``_DROP_FLOOR`` is left alone, and protected keys are skipped."""
    dropped = 0
    current = size_fn()
    while dropped < max_drops and current > target:
        found = _largest_value(node, protect=protect, depth=0, max_depth=3, floor=floor)
        if found is None:
            break
        owner, key, length = found
        replacement = _MARKER.format(n=length)
        owner[key] = replacement
        # Track size incrementally: re-serializing a megabyte document once per blanked value was
        # the entire cost of bounding a huge result (attack round 19, 2026-10-19).
        current -= max(0, length - len(replacement))
        dropped += 1
    return dropped


def _largest_value(node: Any, *, protect: frozenset[str], depth: int, max_depth: int,
                   floor: int) -> tuple[Any, Any, int] | None:
    """(container, key, length) of the longest string value above *floor*, protected keys skipped."""
    best: tuple[Any, Any, int] | None = None
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key) in protect:
                continue
            if isinstance(value, str):
                if len(value) > floor and (best is None or len(value) > best[2]):
                    best = (node, key, len(value))
            elif depth < max_depth and isinstance(value, (dict, list)):
                found = _largest_value(value, protect=protect, depth=depth + 1, max_depth=max_depth, floor=floor)
                if found is not None and (best is None or found[2] > best[2]):
                    best = found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                if len(value) > floor and (best is None or len(value) > best[2]):
                    best = (node, index, len(value))
            elif depth < max_depth and isinstance(value, (dict, list)):
                found = _largest_value(value, protect=protect, depth=depth + 1, max_depth=max_depth, floor=floor)
                if found is not None and (best is None or found[2] > best[2]):
                    best = found
    return best


def _bound_string_value(value: str, *, value_budget: int, digest: str, depth: int, max_depth: int,
                        protect: frozenset[str]) -> str | None:
    """Bound one over-long string value; None when nothing may be changed safely.

    A string field that itself holds JSON (MCP results nest ``{"result": "<json>"}``) must be
    shrunk *inside* it: character-cutting it leaves text nobody can parse, which is exactly the
    defect the independent review surfaced. When the inner document cannot be shrunk (already
    trimmed, or past the depth limit) the value is left untouched — blanking it wholesale stays
    the caller's decision, and the spill keeps the original either way.
    """
    looks_like_json = value.lstrip()[:1] in "{["
    if looks_like_json and depth < max_depth:
        try:
            doc = json.loads(value)
        except Exception:
            doc = None
        if isinstance(doc, (dict, list)):
            if _shrink_values(doc, value_budget=value_budget, digest=digest,
                              depth=depth + 1, max_depth=max_depth, protect=protect):
                return json.dumps(doc, ensure_ascii=False)
            return None  # a JSON document we cannot shrink must not be cut into
    if looks_like_json:
        return None  # past the depth limit: same rule
    return value[:value_budget] + "\n" + _MARKER.format(n=len(value) - value_budget)


def _shrink_values(node: Any, *, value_budget: int, digest: str, depth: int, max_depth: int,
                   protect: frozenset[str]) -> int:
    """Trim over-long strings in place (JSON-document strings shrunk inside); returns the count."""
    trimmed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key) in protect:
                continue
            if isinstance(value, str):
                if len(value) > value_budget:
                    bounded = _bound_string_value(value, value_budget=value_budget, digest=digest,
                                                  depth=depth, max_depth=max_depth, protect=protect)
                    if bounded is not None:
                        node[key] = bounded
                        trimmed += 1
            elif depth < max_depth and isinstance(value, (dict, list)):
                trimmed += _shrink_values(value, value_budget=value_budget, digest=digest,
                                          depth=depth + 1, max_depth=max_depth, protect=protect)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                if len(value) > value_budget:
                    bounded = _bound_string_value(value, value_budget=value_budget, digest=digest,
                                                  depth=depth, max_depth=max_depth, protect=protect)
                    if bounded is not None:
                        node[index] = bounded
                        trimmed += 1
            elif depth < max_depth and isinstance(value, (dict, list)):
                trimmed += _shrink_values(value, value_budget=value_budget, digest=digest,
                                          depth=depth + 1, max_depth=max_depth, protect=protect)
    return trimmed


def build_replacement(tool_name: str, text: str, args: Any, spill: Path, digest: str, *,
                      cfg: dict[str, Any], budget: int, is_error: bool) -> tuple[str, dict[str, Any]]:
    """Return (replacement_text, meta) for one over-budget result.

    meta carries the numbers both the marker and the ledger need, keeping the hook a
    thin wiring layer.
    """
    if len(text) <= budget:
        # In production the caller filters these out; the guard keeps the function honest when
        # used stand-alone: never rewrite, and never claim "over budget" for a payload that
        # fits (fuzz round, 2026-10-18).
        return text, {"structure": "skipped", "omitted_lines": 0,
                      "total_lines": count_lines(text), "head_lines": 0, "tail_lines": 0}
    head_chars, tail_chars = int(cfg["head_chars"]), int(cfg["tail_chars"])
    envelope = unwrap_envelope(text)
    structure = "flat"

    def finish(out: str, meta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Self-guard: never hand back a replacement that is not actually shorter.

        The caller checks this too, but the module is used stand-alone by tests and by any
        future caller, and a marker on a small input can otherwise grow the payload.
        """
        if len(out) >= len(text):
            return text, {"structure": "skipped", "omitted_lines": 0, "total_lines": count_lines(text),
                          "head_lines": 0, "tail_lines": 0}
        return out, meta

    if envelope is not None:
        obj, field = envelope
        payload = obj[field]
        orig_payload_len = len(payload)
        # A field that is itself a JSON document (an MCP result wraps {"result": "<json>"}):
        # trim its long string values rather than cutting it, so every level stays parseable.
        shrunk = shrink_json_document(payload, value_budget=int(cfg.get("json_value_chars") or 2000),
                                      digest=digest, target_chars=budget)
        if shrunk is not None:
            bounded, trimmed_values = shrunk
            total = count_lines(bounded)
            head_n = tail_n = 0
            omitted = 0
            structure = f"json:{field}:values"
            hint = retrieval_hint(tool_name, args, spill, lines_usable=True, head_lines=0)
            kept = (f"字段 {field} 内是 JSON 文档，已截断 {trimmed_values} 个超长值，"
                    f"结构完整、仍可直接解析")
        else:
            head, tail, head_n, tail_n, total = bound_lines(payload, head_chars=head_chars, tail_chars=tail_chars)
            omitted = max(0, total - head_n - tail_n)
            if omitted > 0:
                structure = f"json:{field}"
            else:
                # A single long line inside the field: fall back to a character cut.
                head, tail, complete = bound_chars(payload, head_chars=head_chars, tail_chars=tail_chars)
                if complete:
                    return text, {"structure": "skipped", "omitted_lines": 0, "total_lines": total,
                                  "head_lines": head_n, "tail_lines": tail_n}
                head_n = tail_n = 0
                structure = f"json:{field}:chars"
            lines_usable = structure == f"json:{field}"
            sidecar: Optional[Path] = None
            if omitted > 0 or structure.endswith(":chars"):
                # A plain-text copy of just this field, so the suggested offset addresses the
                # same text the marker counts. Failure is non-fatal: the hint falls back to the spill.
                try:
                    sidecar = spill.with_name(f"{digest}.field.txt")
                    if not sidecar.exists():
                        with sidecar.open("w", encoding="utf-8", errors="surrogatepass", newline="") as handle:
                            handle.write(obj[field])
                except Exception:
                    sidecar = None
            hint = retrieval_hint(tool_name, args, spill, lines_usable=lines_usable, head_lines=head_n,
                                  source_offset=_source_offset(args), sidecar=sidecar)
            if omitted > 0:
                bounded = f"{head}\n… [ops-lite 省略 {omitted:,} 行；全文 sha1 {digest}] …\n{tail}"
                kept = f"前 {head_n} 行 + 后 {tail_n} 行，省略 {omitted:,} 行"
            else:
                bounded = f"{head}\n… [ops-lite 已省略中间字符；全文 sha1 {digest}] …\n{tail}"
                kept = "该字段无换行，已按字符保留头尾"
        rebuilt: dict[str, Any] = {
            "_ops_lite": {
                "note": (f"{tool_name} 结果 {len(text):,} 字符，字段 {field} 原 {orig_payload_len:,} 字符 / "
                         f"{count_lines(obj[field]):,} 行，超出预算 {budget:,}"),
                "kept": kept,
                "full_text": str(spill),
                "sha1": digest,
                "retrieve": hint,
                "was_error": is_error,
            }
        }
        for key, value in obj.items():
            if key == "_ops_lite":
                # Reserve the provenance key: a payload carrying its own "_ops_lite" must not
                # shadow the block above (fuzz round, 2026-10-18). Original stays in the spill.
                continue
            rebuilt[key] = bounded if key == field else value
        serialized = json.dumps(rebuilt, ensure_ascii=False)
        # Other envelope fields can carry as much text as the payload we just bounded (MCP
        # results duplicate the content in a second field). Bound *those* — the payload and the
        # recovery block keep their place, because a duplicate field giving way costs nothing
        # while blanking the payload would cost everything.
        if len(serialized) > budget:
            others = {key: value for key, value in rebuilt.items() if key not in {"_ops_lite", field}}
            if others:
                need = len(serialized) - budget
                others_text = json.dumps(others, ensure_ascii=False)
                extra = shrink_json_document(others_text, value_budget=int(cfg.get("json_value_chars") or 2000),
                                             digest=digest, target_chars=max(0, len(others_text) - need),
                                             protect=frozenset({"_ops_lite"}))
                if extra is not None:
                    bounded_others, _ = extra
                    try:
                        merged = json.loads(bounded_others)
                    except Exception:
                        merged = None
                    if isinstance(merged, dict):
                        rebuilt = {"_ops_lite": rebuilt["_ops_lite"], field: bounded}
                        rebuilt.update(merged)
                        serialized = json.dumps(rebuilt, ensure_ascii=False)
                        structure = f"{structure}+envelope"
        return finish(serialized, {
            "structure": structure, "total_lines": total, "head_lines": head_n,
            "tail_lines": tail_n, "omitted_lines": omitted, "retrieve": hint,
        })

    # A valid JSON document that is not a dict-with-one-big-string-field — a top-level array, or
    # an object made of many medium values — still must not be char-cut: that produced text
    # nobody could parse (independent review, 2026-10-16). Bound it inside and keep it loadable.
    probe = text.lstrip("\ufeff").lstrip()
    if spill is not None and probe[:1] in "{[":
        # Detect by PARSING, not by line count: line count is not a JSON concept (U+2028/U+2029
        # and friends split lines inside strings), and asking for "<= 2 lines" sent pretty-printed
        # documents and separator-bearing arrays down the line/char path, whose output nobody can
        # parse (attack round 3, 2026-10-19).
        try:
            parsed_top = json.loads(probe)
        except Exception:
            parsed_top = None
        if isinstance(parsed_top, (dict, list)):
            meta_block = {
                "note": f"{tool_name} 结果 {len(text):,} 字符（JSON 文档），超出预算 {budget:,}",
                "kept": "",
                "full_text": str(spill), "sha1": digest,
                "retrieve": retrieval_hint(tool_name, args, spill, lines_usable=False, head_lines=0),
                "was_error": is_error,
            }
            document = None
            shrunk_doc = shrink_json_document(probe, value_budget=int(cfg.get("json_value_chars") or 2000),
                                              digest=digest, target_chars=budget, drop_floor=20)
            if shrunk_doc is not None:
                bounded_doc, trimmed_values = shrunk_doc
                try:
                    parsed = json.loads(bounded_doc)
                except Exception:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    meta_block["kept"] = f"已收敛 {trimmed_values} 处超长值，结构完整、仍可直接解析"
                    document = ({**parsed, "_ops_lite": meta_block} if isinstance(parsed, dict)
                                else {"_ops_lite": meta_block, "items": parsed})
            if document is None:
                # Nothing long enough to trim (every value already short): compact the whitespace
                # instead of cutting — a pretty-printed document loses nothing but air.
                compact = json.dumps(parsed_top, ensure_ascii=False, separators=(",", ":"))
                if len(compact) >= len(probe):
                    return text, {"structure": "skipped", "omitted_lines": 0,
                                  "total_lines": count_lines(text), "head_lines": 0, "tail_lines": 0}
                meta_block["kept"] = "该文档没有可收敛的长值，已压缩空白（结构完整、仍可直接解析）"
                document = ({**parsed_top, "_ops_lite": meta_block} if isinstance(parsed_top, dict)
                            else {"_ops_lite": meta_block, "items": parsed_top})
            serialized = json.dumps(document, ensure_ascii=False)
            return finish(serialized, {
                "structure": "json:document", "total_lines": count_lines(serialized),
                "head_lines": 0, "tail_lines": 0, "omitted_lines": 0,
                "retrieve": meta_block["retrieve"],
            })

    # Not an envelope: bound the raw text.
    char_cut = False
    if count_lines(text) <= 2:
        head, tail, complete = bound_chars(text, head_chars=head_chars, tail_chars=tail_chars)
        if complete:
            return text, {"structure": "skipped", "omitted_lines": 0, "total_lines": 1,
                          "head_lines": 0, "tail_lines": 0}
        head_n = tail_n = 0
        total = 1
        structure = "chars"
        char_cut = True
        hint = retrieval_hint(tool_name, args, spill, lines_usable=False, head_lines=0)
    else:
        head, tail, head_n, tail_n, total = bound_lines(text, head_chars=head_chars, tail_chars=tail_chars)
        structure = "lines"
        hint = retrieval_hint(tool_name, args, spill, lines_usable=True, head_lines=head_n,
                              source_offset=_source_offset(args))

    omitted = 0 if char_cut else max(0, total - head_n - tail_n)
    kind = "错误输出" if is_error else "输出"
    if char_cut:
        detail = "该结果无换行，已按字符保留头尾（中段省略）"
    elif omitted:
        detail = f"已收敛为前 {head_n} 行 + 后 {tail_n} 行，省略 {omitted:,} 行"
    else:
        detail = "收敛后未丢失内容"
    parts = [
        f"[ops-lite] {tool_name} {kind} {len(text):,} 字符 / {total:,} 行，超出预算 {budget:,}；{detail}。",
        f"  full : {spill}",
        f"  sha1 : {digest}",
        f"  取回 : {hint}",
        "---- head ----",
        head.rstrip("\n"),
    ]
    if tail:
        parts += ["---- tail ----", tail.rstrip("\n")]
    return finish("\n".join(parts) + "\n", {
        "structure": structure, "total_lines": total, "head_lines": head_n,
        "tail_lines": tail_n, "omitted_lines": omitted, "retrieve": hint,
    })


def repeat_marker(tool_name: str, spill: Path, digest: str, *, chars: int, total: int) -> str:
    return (
        f"[ops-lite] {tool_name} 这次调用的参数与上一次相同，结果未变化"
        f"（sha1 {digest}，{chars:,} 字符 / {total:,} 行），未重复回显。\n"
        f"  full : {spill}\n"
        f"  用法 : 直接复用上一轮已读到的内容；确需重读时 read_file(path=\"{spill}\", offset=1)\n"
    )


def _brief(value: str, limit: int = 80) -> str:
    """One-line, control-free excerpt for the ledger (audit trail, not display)."""
    text = " ".join((value or "").split())
    return text[:limit]


def _first_hit(phrases: tuple[str, ...], haystack: str) -> Optional[str]:
    for phrase in phrases:
        if phrase in haystack:
            return phrase
    return None


def classify_effect_detail(tool_name: str, text: str, *, error_message: str | None = None,
                           is_error: bool = False) -> tuple[Optional[str], Optional[str]]:
    """``classify_effect`` plus the evidence for the verdict: the phrase that decided it.

    A label nobody can audit is a label nobody should trust — the ledger records *why* a result
    was called ``unknown`` (a matched phrase, or ``no-pattern:`` plus the tool's own wording).
    """
    if not is_error:
        return None, None
    tool = (tool_name or "").strip()
    # Measured on this machine (2026-10-20 stress round): 200 KB error → 1.7 ms, 2 KB → 0.01 ms.
    # A compiled alternation regex looked cheaper but ran 26 ms on the same input (CPython tries
    # every branch at every position), so the plain lower()+substring scan stays.
    haystack = f"{error_message or ''}\n{text or ''}".lower()
    if tool in EFFECT_SHELL_TOOLS:
        hit = _first_hit(_EFFECT_UNKNOWN, haystack)
        return ("unknown", hit) if hit else (None, None)
    if tool not in EFFECT_TOOLS:
        return None, None
    hit = _first_hit(_EFFECT_APPLIED, haystack)
    if hit:
        return "applied", hit
    hit = _first_hit(_EFFECT_NONE, haystack)
    if hit:
        return "none", hit
    return "unknown", "no-pattern: " + _brief(error_message or text)


def classify_effect(tool_name: str, text: str, *, error_message: str | None = None,
                    is_error: bool = False) -> Optional[str]:
    """Return ``none`` / ``applied`` / ``unknown`` for a failed write, or None to stay silent.

    Silent is the default everywhere: a read-only failure has no effect to report, and a shell
    command that merely exited non-zero (``grep`` finding nothing) must not be dressed up as an
    unknown write. Only two families get a label:

    * file-writing tools — ``write_file``, ``patch``, ``skill_manage``: the three states, because
      these tools themselves know and state whether the file changed;
    * command tools — ``terminal``, ``execute_code``: ``unknown`` only when the text shows the
      call was cut off (timeout, kill, broken pipe). Their exit codes say nothing about side
      effects, so a plain failure stays unlabelled rather than crying wolf.
    """
    return classify_effect_detail(tool_name, text, error_message=error_message, is_error=is_error)[0]


def annotate_effect(text: str, state: str) -> str:
    """Label a result with its effect state, keeping a JSON envelope parseable.

    A JSON object gets the label as a ``_ops_effect`` key so the model can still parse the
    result; anything else gets one trailing line. Idempotent, and never returns a text that
    already carries a label twice.
    """
    next_step = EFFECT_NEXT.get(state)
    if not next_step or not isinstance(text, str):
        return text
    if "_ops_effect" in text or "[ops-lite 效果：" in text:
        return text
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        merged = dict(parsed)
        merged["_ops_effect"] = {"state": state, "next": next_step}
        try:
            rendered = json.dumps(merged, ensure_ascii=False)
        except Exception:
            rendered = None
        # Re-serializing must not cost real space; otherwise the one-line form is cheaper.
        if rendered is not None and len(rendered) <= len(text) + 512:
            return rendered
    return f"{text.rstrip()}\n[ops-lite 效果：{state} · {next_step}]"


def append_ledger(path: Path, row: dict[str, Any]) -> None:
    """Append one JSON line, atomically enough for concurrent writers.

    Text-mode ``open("a")`` emits the line in pieces on Windows, so two writers can interleave and
    a line disappears (measured: 20 concurrent appends produced 19 lines). One ``os.write`` of the
    pre-encoded bytes under ``O_APPEND`` lands as a unit (attack round 12, 2026-10-19).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8", "surrogatepass")
    with _LEDGER_LOCK:
        for attempt in (0, 1):
            try:
                handle = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0))
                try:
                    os.write(handle, blob)
                finally:
                    os.close(handle)
                return
            except OSError:
                if attempt:
                    raise


def read_ledger(path: Path, since: float | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):  # a hand-edited ledger can hold any JSON value
                    continue
                if since is not None:
                    try:
                        if float(row.get("ts") or 0) < since:
                            continue
                    except (TypeError, ValueError):
                        continue
                rows.append(row)
    except OSError:
        return rows
    return rows


def prune_spill(spill_dir: Path, *, max_files: int, ttl_hours: float) -> int:
    """Drop spill files beyond the newest ``max_files`` or older than ``ttl_hours``."""
    if not spill_dir.exists():
        return 0
    now = time.time()
    entries: list[tuple[float, Path]] = []
    for path in list(spill_dir.glob("*.txt")) + list(spill_dir.glob("*.part")):
        try:
            entries.append((path.stat().st_mtime, path))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    for index, (mtime, path) in enumerate(entries):
        too_many = index >= max_files
        too_old = ttl_hours > 0 and now - mtime > ttl_hours * 3600
        if not (too_many or too_old):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _human_chars(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def summarize(rows: Iterable[dict[str, Any]], *, title: str) -> str:
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return f"ops-lite · {title}: 没有记录（工具输出都还在预算内，或插件刚启用）。"
    bounded = [r for r in rows if r.get("mode") == "bounded"]
    repeat = [r for r in rows if r.get("mode") == "repeat"]
    effects = [r for r in rows if r.get("mode") == "effect"]
    saved = sum(int(r.get("saved_chars") or 0) for r in rows)
    out = [
        f"ops-lite · {title}",
        f"  收敛调用 {len(bounded) + len(repeat):,} 次（超预算收敛 {len(bounded)}，重复短路 {len(repeat)}），"
        f"省下 {_human_chars(saved)} 字符 ≈ {saved / 3.6:,.0f} tok（估算，chars/3.6）",
        "  这些省下的量会随会话每轮重发，实际收益按剩余轮数放大",
    ]
    if effects:
        counts = {name: 0 for name in ("none", "applied", "unknown")}
        for row in effects:
            state = str(row.get("state") or "unknown")
            counts[state if state in counts else "unknown"] += 1
        out.append(f"  效果判定 {len(effects):,} 次：未写入 {counts['none']} · 已写入 {counts['applied']}"
                   f" · 未知 {counts['unknown']}（标 unknown 的不要直接重试）")
    by_tool: dict[str, int] = {}
    for row in rows:
        if row.get("mode") == "effect":
            continue
        by_tool[str(row.get("tool_name") or "?")] = by_tool.get(str(row.get("tool_name") or "?"), 0) + int(row.get("saved_chars") or 0)
    top = sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)[:8]
    if top:
        out.append("  按工具（节省字符）:")
        for name, value in top:
            out.append(f"    {name:28s} {_human_chars(value):>8s}")
    return "\n".join(out)
