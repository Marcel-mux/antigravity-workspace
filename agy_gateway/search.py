"""SQLite FTS5 session search across Antigravity CLI and Hermes Agent histories."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+", re.UNICODE)


@dataclass
class SessionHit:
    """A single session match returned by session search."""

    source: str
    session_id: str
    title: str
    snippet: str
    timestamp: str = ""


def _build_fts_query(raw: str) -> str:
    tokens = FTS_TOKEN_RE.findall(raw.strip())
    if not tokens:
        return ""
    return " ".join(f'"{tok}"' for tok in tokens)


def _clean_snippet(text: str, max_len: int = 180) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def _format_unix_ts(ts: float | int | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)


def search_antigravity_sessions(
    db_path: Path, query: str, *, limit: int = 5
) -> list[SessionHit]:
    """Search Antigravity CLI sessions via `conversations_fts` and `conversation_summaries`."""
    if not db_path.is_file():
        return []
    fts_query = _build_fts_query(query)
    hits: list[SessionHit] = []
    seen_ids: set[str] = set()

    try:
        con = _open_readonly(db_path)
    except sqlite3.Error:
        return []

    try:
        if fts_query:
            try:
                rows = con.execute(
                    """
                    SELECT
                        conversation_id,
                        title,
                        last_modified_time,
                        COALESCE(NULLIF(user_body, ''), agent_body, '') AS body
                    FROM conversations_fts
                    WHERE conversations_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit * 3),
                ).fetchall()
                for conv_id, title, mod_time, body in rows:
                    if conv_id in seen_ids:
                        continue
                    seen_ids.add(conv_id)
                    hits.append(
                        SessionHit(
                            source="antigravity",
                            session_id=str(conv_id),
                            title=str(title or "(untitled)"),
                            snippet=_clean_snippet(str(body or "")),
                            timestamp=str(mod_time or "")[:16].replace("T", " "),
                        )
                    )
                    if len(hits) >= limit:
                        return hits
            except sqlite3.Error:
                pass

        tokens = FTS_TOKEN_RE.findall(query.strip()) or [query.strip()]
        conds = " AND ".join("(title LIKE ? OR preview LIKE ?)" for _ in tokens)
        params: list[object] = []
        for tok in tokens:
            pat = f"%{tok}%"
            params.extend([pat, pat])
        params.append(limit)
        rows = con.execute(
            f"""
            SELECT conversation_id, title, preview, last_modified_time
            FROM conversation_summaries
            WHERE {conds}
            ORDER BY last_modified_time DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        for conv_id, title, preview, mod_time in rows:
            if conv_id in seen_ids:
                continue
            seen_ids.add(conv_id)
            hits.append(
                SessionHit(
                    source="antigravity",
                    session_id=str(conv_id),
                    title=str(title or "(untitled)"),
                    snippet=_clean_snippet(str(preview or "")),
                    timestamp=str(mod_time or "")[:16].replace("T", " "),
                )
            )
            if len(hits) >= limit:
                break
    except sqlite3.Error:
        return hits
    finally:
        con.close()

    return hits


def search_hermes_sessions(
    db_path: Path, query: str, *, limit: int = 5
) -> list[SessionHit]:
    """Search Hermes Agent sessions via `messages_fts` (falling back to LIKE)."""
    if not db_path.is_file():
        return []
    fts_query = _build_fts_query(query)
    hits: list[SessionHit] = []
    seen_ids: set[str] = set()

    try:
        con = _open_readonly(db_path)
    except sqlite3.Error:
        return []

    try:
        if fts_query:
            try:
                rows = con.execute(
                    """
                    SELECT
                        s.id,
                        COALESCE(NULLIF(s.title, ''), s.source, 'hermes'),
                        m.content,
                        m.timestamp
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE messages_fts MATCH ?
                    ORDER BY m.timestamp DESC
                    LIMIT ?
                    """,
                    (fts_query, limit * 3),
                ).fetchall()
                for sid, title, content, ts in rows:
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    hits.append(
                        SessionHit(
                            source="hermes",
                            session_id=str(sid),
                            title=str(title),
                            snippet=_clean_snippet(str(content or "")),
                            timestamp=_format_unix_ts(ts),
                        )
                    )
                    if len(hits) >= limit:
                        return hits
            except sqlite3.Error:
                pass

        like_pat = f"%{query.strip()}%"
        rows = con.execute(
            """
            SELECT
                s.id,
                COALESCE(NULLIF(s.title, ''), s.source, 'hermes'),
                m.content,
                m.timestamp
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.content LIKE ?
            ORDER BY m.timestamp DESC
            LIMIT ?
            """,
            (like_pat, limit * 2),
        ).fetchall()
        for sid, title, content, ts in rows:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            hits.append(
                SessionHit(
                    source="hermes",
                    session_id=str(sid),
                    title=str(title),
                    snippet=_clean_snippet(str(content or "")),
                    timestamp=_format_unix_ts(ts),
                )
            )
            if len(hits) >= limit:
                break
    except sqlite3.Error:
        return hits
    finally:
        con.close()

    return hits


def format_search_results(query: str, hits: list[SessionHit]) -> str:
    """Format session search results for Discord output."""
    if not hits:
        return f"🔍 Tidak ada histori sesi yang cocok dengan `{query}`."

    lines = [f"🔍 **Hasil pencarian sesi untuk `{query}`:**"]
    for idx, hit in enumerate(hits, 1):
        ts_part = f" • {hit.timestamp}" if hit.timestamp else ""
        badge = "AGY" if hit.source == "antigravity" else "HERMES"
        lines.append(
            f"{idx}. **[{badge}] {hit.title}**{ts_part}\n"
            f"   ID: `{hit.session_id}`\n"
            f"   > {hit.snippet or '*(tanpa preview)*'}"
        )
    if any(h.source == "antigravity" for h in hits):
        lines.append("\n💡 Gunakan `/resume <ID>` untuk melanjutkan sesi Antigravity di channel ini.")
    return "\n".join(lines)


def get_antigravity_session_detail(
    db_path: Path, conversation_id: str, *, max_chars: int = 8000
) -> str | None:
    """Return title, metadata, and chunked conversation text for an Antigravity session ID."""
    if not db_path.is_file() or not conversation_id.strip():
        return None
    cid = conversation_id.strip()
    try:
        con = _open_readonly(db_path)
    except sqlite3.Error:
        return None

    try:
        meta = con.execute(
            """
            SELECT title, preview, step_count, last_modified_time, workspace_uris
            FROM conversation_summaries
            WHERE conversation_id = ?
            """,
            (cid,),
        ).fetchone()
        chunks = con.execute(
            """
            SELECT chunk_index, user_body, agent_body
            FROM conversations_fts
            WHERE conversation_id = ?
            ORDER BY chunk_index ASC
            """,
            (cid,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()

    if not meta and not chunks:
        return None

    title = (meta[0] if meta else "") or "(untitled)"
    preview = (meta[1] if meta else "") or ""
    steps = meta[2] if meta else 0
    mod_time = str(meta[3] if meta else "")[:19].replace("T", " ")
    workspaces = (meta[4] if meta else "") or ""

    parts = [
        f"[Antigravity Session: {cid}]",
        f"Title: {title}",
        f"Updated: {mod_time} | Steps: {steps} | Workspaces: {workspaces}",
    ]
    if chunks:
        parts.append("\n--- Indexed Transcript Chunks ---")
        used = sum(len(p) for p in parts)
        for idx, user_body, agent_body in chunks:
            block = f"\n[Chunk {idx}]"
            if user_body:
                block += f"\nUser: {str(user_body).strip()}"
            if agent_body:
                block += f"\nAgent: {str(agent_body).strip()}"
            if used + len(block) > max_chars:
                remaining = max(0, max_chars - used - 20)
                if remaining > 80:
                    parts.append(block[:remaining] + "\n…(truncated)")
                break
            parts.append(block)
            used += len(block)
    elif preview:
        parts.append(f"\nPreview:\n{preview[:max_chars]}")

    return "\n".join(parts)


def get_hermes_session_detail(
    db_path: Path, session_id: str, *, message_limit: int = 20, max_chars: int = 8000
) -> str | None:
    """Return session metadata and recent messages from Hermes `state.db`."""
    if not db_path.is_file() or not session_id.strip():
        return None
    sid = session_id.strip()
    try:
        con = _open_readonly(db_path)
    except sqlite3.Error:
        return None

    try:
        meta = con.execute(
            """
            SELECT source, COALESCE(NULLIF(title, ''), source, 'hermes'), started_at, cwd
            FROM sessions
            WHERE id = ?
            """,
            (sid,),
        ).fetchone()
        rows = con.execute(
            """
            SELECT role, content, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sid, message_limit),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()

    if not meta and not rows:
        return None

    source = (meta[0] if meta else "") or "hermes"
    title = (meta[1] if meta else "") or "(untitled)"
    started = _format_unix_ts(meta[2]) if meta else ""
    cwd = (meta[3] if meta else "") or ""

    parts = [
        f"[Hermes Session: {sid}]",
        f"Source: {source} | Title: {title} | Started: {started} | CWD: {cwd}",
        "\n--- Recent Messages ---",
    ]
    used = sum(len(p) for p in parts)
    for role, content, ts in reversed(rows):
        text = str(content or "").strip()
        if not text:
            continue
        ts_str = _format_unix_ts(ts)
        line = f"[{ts_str}] {role}: {text}"
        if used + len(line) > max_chars:
            remaining = max(0, max_chars - used - 20)
            if remaining > 60:
                parts.append(line[:remaining] + "…")
            break
        parts.append(line)
        used += len(line)

    return "\n".join(parts)
