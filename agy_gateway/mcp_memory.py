#!/usr/bin/env python3
"""Stdio MCP server exposing persistent Agy memory and cross-session FTS5 search."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure workspace package imports work regardless of CWD when launched by `agy`.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from agy_gateway.search import (
    get_antigravity_session_detail,
    get_hermes_session_detail,
    search_antigravity_sessions,
    search_hermes_sessions,
)
from agy_gateway.skill_sync import sync_hermes_skills

DEFAULT_MEMORY_FILE = Path.home() / ".gemini" / "config" / "AGY_MEMORY.md"
DEFAULT_AGY_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
DEFAULT_HERMES_DB = Path.home() / ".hermes" / "state.db"

SERVER_INFO = {"name": "agy-memory-search", "version": "1.0.0"}
SUPPORTED_PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_read",
        "description": (
            "Read Antigravity's persistent cross-session memory file (AGY_MEMORY.md). "
            "Use when recalling durable facts, decisions, project conventions, or learned fixes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_save",
        "description": (
            "Save a durable fact, decision, preference, or verified fix into AGY_MEMORY.md "
            "so future sessions remember it automatically after /new or /compact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Concise factual entry to persist.",
                },
                "section": {
                    "type": "string",
                    "description": "Optional heading category (default: General).",
                },
            },
            "required": ["note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_remove",
        "description": "Remove entries from AGY_MEMORY.md matching a substring.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "match": {
                    "type": "string",
                    "description": "Case-insensitive substring identifying lines to delete.",
                },
            },
            "required": ["match"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_search",
        "description": (
            "Search past conversation histories across Antigravity CLI (conversation_summaries.db) "
            "and Hermes Agent (state.db) using FTS5."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords."},
                "source": {
                    "type": "string",
                    "enum": ["all", "antigravity", "hermes"],
                    "description": "Which history store to query (default: all).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Max hits per source (default: 5).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_get_detail",
        "description": "Retrieve detailed transcript/summary chunks for a specific session ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session/conversation ID."},
                "source": {
                    "type": "string",
                    "enum": ["antigravity", "hermes"],
                    "description": "Session store (default: antigravity).",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
]


@contextmanager
def _locked_memory(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _memory_read(memory_path: Path) -> str:
    if not memory_path.is_file():
        return "(AGY_MEMORY.md is empty — no entries saved yet.)"
    content = memory_path.read_text(encoding="utf-8").strip()
    return content or "(AGY_MEMORY.md is empty — no entries saved yet.)"


def _memory_save(memory_path: Path, note: str, section: str = "General") -> str:
    clean_note = " ".join(note.strip().split())
    if not clean_note:
        return "Error: note cannot be empty."
    clean_sec = (section or "General").strip() or "General"
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    entry = f"- [{stamp}] {clean_note}"

    with _locked_memory(memory_path):
        existing = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
        if clean_note.lower() in existing.lower():
            return f"Already stored in {memory_path}: {clean_note}"

        if not existing.strip():
            updated = f"# Agy Persistent Memory\n\n## {clean_sec}\n{entry}\n"
        else:
            header = f"## {clean_sec}"
            lines = existing.splitlines()
            if header in lines:
                idx = lines.index(header) + 1
                while idx < len(lines) and not lines[idx].startswith("## "):
                    idx += 1
                lines.insert(idx, entry)
                updated = "\n".join(lines).rstrip() + "\n"
            else:
                updated = f"{existing.rstrip()}\n\n{header}\n{entry}\n"

        _atomic_write(memory_path, updated)
    return f"Saved to {memory_path} under [{clean_sec}]: {entry}"


def _memory_remove(memory_path: Path, match: str) -> str:
    needle = match.strip().lower()
    if not needle:
        return "Error: match cannot be empty."
    with _locked_memory(memory_path):
        if not memory_path.is_file():
            return "No entries found (AGY_MEMORY.md does not exist)."
        lines = memory_path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        removed = 0
        for line in lines:
            if line.lstrip().startswith("- ") and needle in line.lower():
                removed += 1
            else:
                kept.append(line)
        if removed:
            _atomic_write(memory_path, "\n".join(kept).rstrip() + "\n")
    return f"Removed {removed} matching memory entry(ies)."


def _session_search(
    agy_db: Path, hermes_db: Path, query: str, source: str = "all", limit: int = 5
) -> str:
    q = query.strip()
    if not q:
        return "Error: query cannot be empty."
    lim = max(1, min(20, int(limit)))
    hits = []
    if source in ("all", "antigravity"):
        hits.extend(search_antigravity_sessions(agy_db, q, limit=lim))
    if source in ("all", "hermes"):
        hits.extend(search_hermes_sessions(hermes_db, q, limit=lim))
    if not hits:
        return f"No sessions matched query: {q}"
    lines = []
    for idx, h in enumerate(hits, 1):
        ts = f" ({h.timestamp})" if h.timestamp else ""
        lines.append(f"{idx}. [{h.source}] {h.title}{ts}\n   id: {h.session_id}\n   snippet: {h.snippet}")
    return "\n".join(lines)


def _session_detail(agy_db: Path, hermes_db: Path, session_id: str, source: str = "antigravity") -> str:
    if source == "hermes":
        detail = get_hermes_session_detail(hermes_db, session_id)
    else:
        detail = get_antigravity_session_detail(agy_db, session_id)
        if detail is None and source == "antigravity":
            detail = get_hermes_session_detail(hermes_db, session_id)
    return detail or f"Session '{session_id}' not found."


def handle_rpc_request(
    req: dict[str, Any],
    *,
    memory_path: Path = DEFAULT_MEMORY_FILE,
    agy_db: Path = DEFAULT_AGY_DB,
    hermes_db: Path = DEFAULT_HERMES_DB,
) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 MCP message and return response dict (or None for notifications)."""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # Notifications (no id) require no response.
    if req_id is None:
        return None

    if method == "initialize":
        proto = str(params.get("protocolVersion") or SUPPORTED_PROTOCOL)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            if name == "memory_read":
                text = _memory_read(memory_path)
            elif name == "memory_save":
                text = _memory_save(
                    memory_path,
                    str(args.get("note") or ""),
                    str(args.get("section") or "General"),
                )
            elif name == "memory_remove":
                text = _memory_remove(memory_path, str(args.get("match") or ""))
            elif name == "session_search":
                text = _session_search(
                    agy_db,
                    hermes_db,
                    str(args.get("query") or ""),
                    str(args.get("source") or "all"),
                    int(args.get("limit") or 5),
                )
            elif name == "session_get_detail":
                text = _session_detail(
                    agy_db,
                    hermes_db,
                    str(args.get("session_id") or ""),
                    str(args.get("source") or "antigravity"),
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    try:
        sync_hermes_skills()
    except Exception:  # noqa: BLE001
        pass

    memory_path = Path(os.getenv("AGY_MEMORY_FILE", str(DEFAULT_MEMORY_FILE))).expanduser()
    agy_db = Path(os.getenv("AGY_CONVERSATIONS_DB", str(DEFAULT_AGY_DB))).expanduser()
    hermes_db = Path(os.getenv("HERMES_STATE_DB", str(DEFAULT_HERMES_DB))).expanduser()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue
        resp = handle_rpc_request(
            req,
            memory_path=memory_path,
            agy_db=agy_db,
            hermes_db=hermes_db,
        )
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
