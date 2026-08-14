"""Claude Code session reader and discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from reader_core import (
    CLAUDE_KNOWN_TYPES,
    UUID_RE,
    ReaderError,
    WorkIndex,
    _add_warning,
    _blocks,
    _content_text,
    _finalize_result,
    _is_generated_meta_text,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _paths_match,
    _read_plain_jsonl,
    _safe_text,
    _timestamp_sort_key,
    _turn,
    _within,
    slugify,
)

CLAUDE_TITLE_FIELDS = (
    ("custom-title", "customTitle"),
    ("ai-title", "aiTitle"),
    ("summary", "summary"),
)


def _claude_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _claude_segment(boundary: dict[str, Any]) -> dict[str, Any] | None:
    metadata = boundary.get("compactMetadata")
    if not isinstance(metadata, dict):
        metadata = boundary.get("compact_metadata")
    if not isinstance(metadata, dict):
        return None
    segment = metadata.get("preservedSegment")
    if not isinstance(segment, dict):
        segment = metadata.get("preserved_segment")
    if not isinstance(segment, dict):
        return None
    return {
        "head": segment.get("headUuid") or segment.get("head_uuid"),
        "anchor": segment.get("anchorUuid") or segment.get("anchor_uuid"),
        "tail": segment.get("tailUuid") or segment.get("tail_uuid"),
    }


def _is_claude_boundary(record: dict[str, Any]) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


def _claude_parent(record: dict[str, Any]) -> str | None:
    uuid = record.get("uuid")
    for field in ("parentUuid", "logicalParentUuid"):
        parent = record.get(field)
        if isinstance(parent, str) and parent and parent != uuid:
            return parent
    return None


def _set_claude_parent(record: dict[str, Any], parent: str | None) -> None:
    record["parentUuid"] = parent
    if "logicalParentUuid" in record:
        record["logicalParentUuid"] = parent


def _prepare_claude_messages(
    records: list[dict[str, Any]], warnings: list[dict[str, str]]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    last_non_preserved = -1
    for index, record in enumerate(records):
        if _is_claude_boundary(record) and _claude_segment(record) is None:
            last_non_preserved = index
    scoped = records[last_non_preserved:] if last_non_preserved >= 0 else records
    messages: dict[str, dict[str, Any]] = {}
    for record in scoped:
        if record.get("isSidechain"):
            continue
        if record.get("type") not in {"user", "assistant", "system"}:
            continue
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid:
            messages[uuid] = dict(record)
    _apply_claude_preserved_segment(messages, warnings)
    _apply_claude_snip_removals(messages)
    return messages, scoped


def _apply_claude_preserved_segment(
    messages: dict[str, dict[str, Any]], warnings: list[dict[str, str]]
) -> None:
    keys = list(messages)
    absolute_boundary_index = -1
    last_segment_index = -1
    last_segment: dict[str, Any] | None = None
    for index, record in enumerate(messages.values()):
        if not _is_claude_boundary(record):
            continue
        absolute_boundary_index = index
        segment = _claude_segment(record)
        if segment is not None:
            last_segment = segment
            last_segment_index = index
    if last_segment is None:
        return
    segment_live = last_segment_index == absolute_boundary_index
    preserved: set[str] = set()
    if segment_live:
        head = last_segment.get("head")
        anchor = last_segment.get("anchor")
        tail = last_segment.get("tail")
        if not all(isinstance(item, str) and item for item in (head, anchor, tail)):
            _add_warning(
                warnings,
                "preserved_segment_unavailable",
                "Preserved-segment metadata was incomplete; pre-compact history was retained.",
            )
            return
        current = messages.get(tail)
        seen: set[str] = set()
        reached_head = False
        while current is not None:
            uuid = current.get("uuid")
            if not isinstance(uuid, str) or uuid in seen:
                break
            seen.add(uuid)
            preserved.add(uuid)
            if uuid == head:
                reached_head = True
                break
            parent = _claude_parent(current)
            current = messages.get(parent) if parent is not None else None
        if not reached_head:
            _add_warning(
                warnings,
                "preserved_segment_unavailable",
                "Preserved-segment messages were missing or cyclic; pre-compact history was retained.",
            )
            return
        _set_claude_parent(messages[head], anchor)
        for uuid, message in messages.items():
            if uuid != head and _claude_parent(message) == anchor:
                _set_claude_parent(message, tail)
    if absolute_boundary_index < 0:
        return
    for uuid in keys[:absolute_boundary_index]:
        if uuid not in preserved:
            messages.pop(uuid, None)


def _apply_claude_snip_removals(messages: dict[str, dict[str, Any]]) -> None:
    removed: set[str] = set()
    for record in messages.values():
        metadata = record.get("snipMetadata")
        if not isinstance(metadata, dict):
            metadata = record.get("snip_metadata")
        values = metadata.get("removedUuids") if isinstance(metadata, dict) else None
        if values is None and isinstance(metadata, dict):
            values = metadata.get("removed_uuids")
        if isinstance(values, list):
            removed.update(value for value in values if isinstance(value, str))
    if not removed:
        return
    deleted_parents: dict[str, str | None] = {}
    for uuid in removed:
        record = messages.pop(uuid, None)
        if record is not None:
            deleted_parents[uuid] = _claude_parent(record)

    def resolve(start: str) -> str | None:
        path: list[str] = []
        current: str | None = start
        seen: set[str] = set()
        while current is not None and current in removed and current not in seen:
            seen.add(current)
            path.append(current)
            current = deleted_parents.get(current)
        for item in path:
            deleted_parents[item] = current
        return current

    for record in messages.values():
        parent = _claude_parent(record)
        if parent is not None and parent in removed:
            _set_claude_parent(record, resolve(parent))


def _claude_leaf(
    graph: dict[str, dict[str, Any]],
    messages: dict[str, dict[str, Any]],
    warnings: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not messages:
        return None
    parent_uuids = {
        parent
        for record in graph.values()
        for parent in [_claude_parent(record)]
        if parent is not None
    }
    candidates: list[dict[str, Any]] = []
    for record in graph.values():
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or uuid in parent_uuids:
            continue
        current: dict[str, Any] | None = record
        seen: set[str] = set()
        while current is not None:
            current_uuid = current.get("uuid")
            if not isinstance(current_uuid, str) or current_uuid in seen:
                _add_warning(
                    warnings,
                    "parent_cycle",
                    "A cycle was detected in the transcript parent chain; only the recoverable suffix is shown.",
                )
                break
            seen.add(current_uuid)
            if current.get("type") in {"user", "assistant"}:
                candidates.append(current)
                break
            parent = _claude_parent(current)
            current = graph.get(parent) if parent is not None else None
    conversation = [
        record for record in messages.values() if record.get("type") in {"user", "assistant"}
    ]
    if not candidates:
        candidates = conversation
    if not candidates:
        return None
    positions = {uuid: index for index, uuid in enumerate(messages)}
    return max(
        candidates,
        key=lambda record: _timestamp_sort_key(
            record, positions.get(str(record.get("uuid")), -1)
        ),
    )


def _claude_chain(
    graph: dict[str, dict[str, Any]],
    messages: dict[str, dict[str, Any]],
    leaf: dict[str, Any],
    warnings: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], set[str]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        uuid = current.get("uuid")
        if not isinstance(uuid, str):
            break
        if uuid in seen:
            _add_warning(
                warnings,
                "parent_cycle",
                "A cycle was detected in the transcript parent chain; only the recoverable suffix is shown.",
            )
            break
        seen.add(uuid)
        chain.append(current)
        parent = _claude_parent(current)
        current = graph.get(parent) if parent is not None else None
    chain.reverse()
    return _recover_claude_parallel(messages, chain, seen), seen


def _recover_claude_parallel(
    messages: dict[str, dict[str, Any]],
    chain: list[dict[str, Any]],
    seen: set[str],
) -> list[dict[str, Any]]:
    chain_assistants = [record for record in chain if record.get("type") == "assistant"]
    if not chain_assistants:
        return chain
    anchors: dict[str, dict[str, Any]] = {}
    siblings: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, list[dict[str, Any]]] = {}
    positions = {uuid: index for index, uuid in enumerate(messages)}
    for assistant in chain_assistants:
        message_id = (assistant.get("message") or {}).get("id")
        if isinstance(message_id, str) and message_id:
            anchors[message_id] = assistant
    for record in messages.values():
        message = record.get("message") or {}
        if record.get("type") == "assistant":
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                siblings.setdefault(message_id, []).append(record)
        elif record.get("type") == "user":
            parent = _claude_parent(record)
            if parent is not None and any(
                block.get("type") == "tool_result"
                for block in _blocks(message.get("content"))
            ):
                results.setdefault(parent, []).append(record)
    inserts: dict[str, list[dict[str, Any]]] = {}
    processed: set[str] = set()
    for assistant in chain_assistants:
        message_id = (assistant.get("message") or {}).get("id")
        if not isinstance(message_id, str) or message_id in processed:
            continue
        processed.add(message_id)
        group = siblings.get(message_id, [assistant])
        orphaned_siblings = [record for record in group if record.get("uuid") not in seen]
        orphaned_results = [
            result
            for member in group
            for result in results.get(str(member.get("uuid")), [])
            if result.get("uuid") not in seen
        ]
        ordering = lambda record: _timestamp_sort_key(
            record, positions.get(str(record.get("uuid")), -1)
        )
        recovered = sorted(orphaned_siblings, key=ordering) + sorted(
            orphaned_results, key=ordering
        )
        if recovered:
            anchor = anchors[message_id]
            inserts[str(anchor.get("uuid"))] = recovered
            seen.update(
                str(record.get("uuid")) for record in recovered if record.get("uuid") is not None
            )
    output: list[dict[str, Any]] = []
    for record in chain:
        output.append(record)
        output.extend(inserts.get(str(record.get("uuid")), []))
    return output


def _claude_replacement_ids(records: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for record in records:
        if record.get("type") != "content-replacement" or record.get("agentId"):
            continue
        replacements = record.get("replacements")
        if not isinstance(replacements, list):
            continue
        for replacement in replacements:
            if not isinstance(replacement, dict):
                continue
            tool_id = replacement.get("toolUseId") or replacement.get("tool_use_id")
            if isinstance(tool_id, str):
                ids.add(tool_id)
    return ids


def _replacement_stub(content: str, tool_use_id: Any, replacement_ids: set[str]) -> bool:
    return (
        isinstance(tool_use_id, str)
        and tool_use_id in replacement_ids
        or "<persisted-output>" in content
        or "[Old tool result content cleared]" in content
    )


def _render_claude_record(
    record: dict[str, Any],
    max_tool_chars: int,
    replacement_ids: set[str],
    index: WorkIndex | None = None,
) -> dict[str, Any] | None:
    if record.get("type") not in {"user", "assistant"}:
        return None
    if any(
        record.get(flag)
        for flag in ("isMeta", "isCompactSummary", "isVirtual", "isVisibleInTranscriptOnly")
    ):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role") if message.get("role") in {"user", "assistant"} else record["type"]
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in _blocks(message.get("content")):
        block_type = block.get("type")
        if block_type in {"thinking", "redacted_thinking", "signature"}:
            continue
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip() and not _is_generated_meta_text(text):
                texts.append(_safe_text(text))
        elif block_type == "tool_use":
            if index is not None:
                index.record(block.get("name"), block.get("input", {}))
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "name": _safe_text(block.get("name") or "unknown"),
                    "input": _json_preview(block.get("input", {}), max_tool_chars),
                    "inert": True,
                }
            )
        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id")
            raw_content = _content_text(block.get("content"))
            if _replacement_stub(raw_content, tool_use_id, replacement_ids):
                content = "[output summarized/stored elsewhere]"
                unavailable = True
            else:
                content = _one_line(raw_content, max_tool_chars)
                unavailable = False
            tool_results.append(
                {
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": bool(block.get("is_error")),
                    "unavailable": unavailable,
                    "inert": True,
                }
            )
        elif block_type == "image":
            texts.append("[image content unavailable]")
    text = "\n".join(item for item in texts if item.strip())
    if not text and not tool_calls and not tool_results:
        return None
    return _turn(role, text=text, tool_calls=tool_calls, tool_results=tool_results)


def _claude_quick_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    if not lines:
        return None

    def parse(line: str) -> dict[str, Any] | None:
        if not line.strip():
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    cwd: str | None = None
    opening: str | None = None
    for line in lines[:80]:
        record = parse(line)
        if record is None:
            continue
        if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
            cwd = record["cwd"]
        if opening is None and record.get("type") == "user" and not record.get("isMeta"):
            message = record.get("message")
            if isinstance(message, dict):
                text = _content_text(message.get("content"))
                if text.strip() and not _is_generated_meta_text(text):
                    opening = text
        if cwd is not None and opening is not None:
            break

    branch: str | None = None
    for line in reversed(lines[-80:]):
        record = parse(line)
        if record is not None and isinstance(record.get("gitBranch"), str):
            branch = record["gitBranch"]
            break

    titles: dict[str, str] = {}
    for line in lines:
        if not any(f'"{name}"' in line for name, _ in CLAUDE_TITLE_FIELDS):
            continue
        record = parse(line)
        if record is None:
            continue
        for name, field in CLAUDE_TITLE_FIELDS:
            if record.get("type") == name and isinstance(record.get(field), str):
                titles[name] = record[field]
    title = next((titles[name] for name, _ in CLAUDE_TITLE_FIELDS if name in titles), None)
    return {
        "cwd": cwd,
        "branch": branch,
        "title": _one_line(title or opening, 200) or None,
    }


def _claude_title(records: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str | None:
    for record_type, field in (
        ("custom-title", "customTitle"),
        ("ai-title", "aiTitle"),
        ("summary", "summary"),
    ):
        values = [
            record.get(field)
            for record in records
            if record.get("type") == record_type and isinstance(record.get(field), str)
        ]
        if values:
            return _one_line(values[-1], 200)
    return next(
        (_one_line(turn["text"], 200) for turn in turns if turn["role"] == "user" and turn["text"]),
        None,
    )


def read_claude_session(
    path: Path | str,
    max_tool_chars: int = 300,
    *,
    tool: str = "claude",
    source: str = "claude-code",
    label: str = "Claude",
    known_types: set[str] = CLAUDE_KNOWN_TYPES,
) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    records, malformed = _read_plain_jsonl(session_path)
    warnings: list[dict[str, str]] = []
    if malformed:
        _add_warning(
            warnings,
            "malformed_records_skipped",
            f"Skipped {malformed} malformed {label} transcript record(s).",
        )
    unknown = sum(
        1
        for record in records
        if isinstance(record.get("type"), str) and record.get("type") not in known_types
    )
    if unknown:
        _add_warning(
            warnings,
            "unknown_records_skipped",
            f"Skipped {unknown} unknown {label} record(s) without interpreting their payloads.",
        )
    messages, scoped = _prepare_claude_messages(records, warnings)
    graph = {
        str(record["uuid"]): record
        for record in scoped
        if isinstance(record.get("uuid"), str) and record["uuid"]
    }
    leaf = _claude_leaf(graph, messages, warnings)
    chain: list[dict[str, Any]] = []
    if leaf is not None:
        chain, _ = _claude_chain(graph, messages, leaf, warnings)
    replacements = _claude_replacement_ids(records)
    index = WorkIndex()
    turns = [
        turn
        for record in chain
        for turn in [_render_claude_record(record, max_tool_chars, replacements, index)]
        if turn is not None
    ]
    metadata_records = chain if chain else records
    cwd = next(
        (
            record.get("cwd")
            for record in metadata_records
            if isinstance(record.get("cwd"), str)
        ),
        None,
    )
    branch = next(
        (
            record.get("gitBranch")
            for record in reversed(metadata_records)
            if isinstance(record.get("gitBranch"), str)
        ),
        None,
    )
    timestamps = [
        record["timestamp"]
        for record in chain
        if isinstance(record.get("timestamp"), str)
    ]
    result = {
        "tool": tool,
        "source": source,
        "session_id": session_path.name.removesuffix(".jsonl"),
        "path": str(session_path),
        "title": _claude_title(records, turns),
        "cwd": cwd,
        "branch": branch,
        "created_at": timestamps[0] if timestamps else None,
        "updated_at": timestamps[-1] if timestamps else _iso_from_millis(_mtime_millis(session_path)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
    }
    return _finalize_result(result, index)


def _discover_claude_layout(
    projects: Path,
    tool: str,
    source: str,
    quick_meta: Callable[[Path], dict[str, Any] | None],
    cwd: str | None,
    within_min: int,
    slug: Callable[[str], str] = slugify,
) -> list[dict[str, Any]]:
    if not projects.is_dir():
        return []
    expected = projects / slug(cwd) if cwd else None
    project_dirs: list[Path] = []
    if expected is not None and expected.is_dir() and not expected.is_symlink():
        project_dirs.append(expected)
    try:
        project_dirs.extend(
            path
            for path in sorted(projects.iterdir(), key=lambda item: item.name)
            if path != expected and path.is_dir() and not path.is_symlink()
        )
    except OSError:
        pass
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in project_dirs:
        try:
            paths = sorted(project.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".jsonl"
                or not UUID_RE.fullmatch(path.stem)
                or path.stem in seen
            ):
                continue
            updated = _mtime_millis(path)
            if not _within(updated, within_min):
                continue
            result = quick_meta(path)
            if result is None:
                continue
            if cwd is not None:
                if result.get("cwd") and not _paths_match(result["cwd"], cwd):
                    continue
                if not result.get("cwd") and project != expected:
                    continue
            seen.add(path.stem)
            sessions.append(
                {
                    "tool": tool,
                    "source": source,
                    "session_id": path.stem,
                    "path": str(path),
                    "title": result.get("title") or "(untitled)",
                    "cwd": result.get("cwd") or cwd,
                    "branch": result.get("branch"),
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                }
            )
    return sessions


def _discover_claude(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    return _discover_claude_layout(
        _claude_config_dir() / "projects",
        "claude",
        "claude-code",
        _claude_quick_meta,
        cwd,
        within_min,
    )


def _find_claude_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    projects = _claude_config_dir() / "projects"
    if not projects.is_dir():
        return None
    candidates = [projects / slugify(cwd) / f"{session_id}.jsonl"]
    candidates.extend(sorted(projects.glob(f"*/{session_id}.jsonl"), key=str))
    for path in candidates:
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("claude", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            if path.is_file() and not path.is_symlink():
                meta = _claude_quick_meta(path)
                return {
                    "tool": "claude",
                    "source": "claude-code",
                    "session_id": session_id,
                    "path": str(path),
                    "title": (meta.get("title") if meta else None) or "(untitled)",
                    "cwd": (meta.get("cwd") if meta else None) or cwd,
                    "branch": meta.get("branch") if meta else None,
                    "updated_at_ms": _mtime_millis(path),
                }
    return None
