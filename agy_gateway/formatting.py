"""Formatting utilities for Discord limits and agy stream-json event parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DISCORD_MAX_LEN = 1950
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


@dataclass
class StreamEvent:
    """Normalized event parsed from an `agy --output-format stream-json` line."""

    event_type: str
    conversation_id: str | None = None
    text_delta: str = ""
    final_response: str | None = None
    step_type: str | None = None
    step_state: str | None = None
    status: str | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    usage: dict[str, int] | None = None


def _parse_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"):
        val = raw.get(key)
        if isinstance(val, (int, float)):
            parsed[key] = int(val)
    return parsed or None


def parse_stream_event(line: str) -> StreamEvent | None:
    """Parse one NDJSON line emitted by `agy --output-format stream-json`."""
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        data: dict[str, Any] = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    event_type = str(data.get("event") or data.get("type") or "")
    conv_id = data.get("conversation_id")

    if event_type == "init":
        return StreamEvent(event_type="init", conversation_id=conv_id)

    if event_type == "step_update":
        step = data.get("step_update") or {}
        return StreamEvent(
            event_type="step_update",
            conversation_id=step.get("conversation_id") or conv_id,
            text_delta=str(step.get("text_delta") or ""),
            step_type=step.get("step_type"),
            step_state=step.get("state"),
            duration_seconds=step.get("duration_seconds"),
            usage=_parse_usage(step.get("usage")),
        )

    if event_type == "result":
        res = data.get("result") or {}
        return StreamEvent(
            event_type="result",
            conversation_id=res.get("conversation_id") or conv_id,
            final_response=res.get("response"),
            status=res.get("status"),
            duration_seconds=res.get("duration_seconds"),
            error_message=res.get("error"),
            usage=_parse_usage(res.get("usage")),
        )

    if event_type == "error":
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err or data)
        return StreamEvent(event_type="error", conversation_id=conv_id, error_message=msg)

    return StreamEvent(event_type=event_type or "unknown", conversation_id=conv_id)


def split_message(text: str, max_len: int = DISCORD_MAX_LEN) -> list[str]:
    """Split text into Discord-safe chunks while preserving fenced code blocks."""
    if not text:
        return ["*(empty response)*"]
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    active_fence: str | None = None

    while remaining:
        prefix = f"{active_fence}\n" if active_fence else ""
        budget = max_len - len(prefix) - 8  # reserve space for closing ```
        if budget < 200:
            budget = max_len

        if len(prefix) + len(remaining) <= max_len:
            chunks.append(prefix + remaining)
            break

        split_at = _find_split_point(remaining, budget)
        part = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")

        # Track open/closed code fences across this part to keep markdown intact.
        fence_state = active_fence
        for line in part.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                fence_state = None if fence_state is not None else stripped

        chunk = prefix + part
        if fence_state is not None:
            chunk = chunk.rstrip("\n") + "\n```"
        chunks.append(chunk)
        active_fence = fence_state

    return chunks


def _find_split_point(text: str, budget: int) -> int:
    if len(text) <= budget:
        return len(text)
    window = text[:budget]
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx > budget // 3:
            return idx
    return budget


def format_streaming_preview(text: str, status_line: str) -> str:
    """Format an in-progress streaming preview message within Discord's 2000-char limit."""
    clean = text.strip()
    if not clean:
        return status_line
    # Leave room for status footer and truncation notice.
    max_body = 1750
    if len(clean) > max_body:
        clean = "…\n" + clean[-max_body:]
    # Close any unclosed triple backtick fence in the preview so Discord renders cleanly.
    if clean.count("```") % 2 == 1:
        clean += "\n```"
    return f"{clean}\n\n{status_line}"


def extract_channel_mentions(
    text: str, *, exclude_channel_id: int | None = None, max_channels: int = 3
) -> list[int]:
    """Extract unique Discord <#channel_id> mentions from text in order of appearance."""
    seen: set[int] = set()
    result: list[int] = []
    for raw_id in CHANNEL_MENTION_RE.findall(text):
        cid = int(raw_id)
        if exclude_channel_id is not None and cid == exclude_channel_id:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        result.append(cid)
        if len(result) >= max_channels:
            break
    return result


def format_referenced_channel_block(
    channel_name: str,
    channel_id: int,
    messages: list[tuple[str, str]],
    *,
    conversation_id: str | None = None,
    max_chars: int = 3500,
) -> str:
    """Format recent messages from a referenced Discord channel/thread for prompt injection."""
    header = f"### #{channel_name} (channel_id={channel_id}"
    if conversation_id:
        header += f", agy_conversation_id={conversation_id}"
    header += ")"

    if not messages:
        return f"{header}\n*(tidak ada riwayat pesan teks)*"

    lines: list[str] = []
    total = 0
    for author, body in messages:
        snippet = body.strip()
        if len(snippet) > 600:
            snippet = snippet[:599] + "…"
        entry = f"- **{author}**: {snippet}"
        if total + len(entry) > max_chars and lines:
            break
        lines.append(entry)
        total += len(entry)

    return f"{header}\n" + "\n".join(lines)


def strip_bot_mention(text: str, bot_user_id: int) -> str:
    """Remove explicit `<@id>` / `<@!id>` mentions of the bot without mangling indentation."""
    cleaned = re.sub(rf"<@!?{bot_user_id}>[ \t]*", "", text)
    return cleaned.strip()


def format_channel_backfill_block(
    channel_name: str,
    messages: list[tuple[str, str]],
    *,
    replied_message: tuple[str, str] | None = None,
    max_chars: int = 4000,
) -> str | None:
    """Format missed recent channel messages (prioritizing newest) for prompt injection."""
    if not messages and replied_message is None:
        return None

    sections: list[str] = []

    if messages:
        # Keep the newest messages within budget, then restore chronological order.
        kept_rev: list[str] = []
        total = 0
        for author, body in reversed(messages):
            snippet = body.strip()
            if not snippet:
                continue
            if len(snippet) > 900:
                snippet = snippet[:899] + "…"
            entry = f"- **{author}**: {snippet}"
            if total + len(entry) > max_chars and kept_rev:
                break
            kept_rev.append(entry)
            total += len(entry)
        if kept_rev:
            kept_rev.reverse()
            sections.append(
                f"[Recent Channel Context in #{channel_name} Since Your Last Reply]\n"
                + "\n".join(kept_rev)
                + "\n[End Recent Channel Context]"
            )

    if replied_message is not None:
        r_author, r_body = replied_message
        r_snippet = r_body.strip()
        if r_snippet:
            if len(r_snippet) > 1200:
                r_snippet = r_snippet[:1199] + "…"
            sections.append(
                f"[Message Replied To]\n- **{r_author}**: {r_snippet}\n[End Message Replied To]"
            )

    return "\n\n".join(sections) if sections else None


def _progress_bar(fraction: float, width: int = 10) -> str:
    clamped = max(0.0, min(1.0, fraction))
    filled = round(clamped * width)
    return "█" * filled + "░" * (width - filled)


def _format_reset_time(raw_iso: str | None) -> str:
    if not raw_iso:
        return ""
    try:
        dt = datetime.fromisoformat(raw_iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return raw_iso


def format_usage_report(payload: dict[str, Any]) -> str:
    """Format `agy -p /usage --output-format json` output for Discord."""
    cmd_data = (payload.get("command") or {}).get("data") or {}
    groups = cmd_data.get("groups")
    if not isinstance(groups, list) or not groups:
        fallback = str(payload.get("response") or "").strip()
        return f"📊 **Antigravity Quota & Usage**\n```\n{fallback or 'Data usage tidak tersedia.'}\n```"

    lines = ["📊 **Antigravity Quota & Usage**"]
    for group in groups:
        name = str(group.get("name") or "Unknown Group")
        desc = str(group.get("description") or "").strip()
        lines.append(f"\n**{name}**")
        if desc and len(desc) <= 300:
            lines.append(f"*{desc}*")

        for bucket in group.get("buckets") or []:
            b_name = str(bucket.get("name") or bucket.get("window") or "Limit")
            frac = float(bucket.get("remaining_fraction") or 0.0)
            pct = frac * 100.0
            bar = _progress_bar(frac)
            reset_str = _format_reset_time(bucket.get("reset_time"))
            reset_part = f" • reset `{reset_str}`" if reset_str else ""
            lines.append(f"- `{bar}` **{pct:.1f}%** — {b_name}{reset_part}")

    return "\n".join(lines)


def infer_context_window_limit(model_id: str | None) -> int | None:
    """Return known context window size in tokens for a model family, or None if unknown."""
    m = (model_id or "").lower()
    if not m or m == "default" or "gemini" in m or "orchestrator" in m:
        return 1_048_576
    if "claude" in m:
        return 200_000
    return None


def _fmt_compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_context_usage(
    context_tokens: int | None,
    input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    output_tokens: int | None = None,
    *,
    model_id: str | None = None,
) -> str:
    """Format per-turn context window usage and cumulative session token stats for `/status`."""
    if context_tokens is None and input_tokens is None:
        return "*(belum ada data di sesi ini)*"

    if context_tokens is not None:
        limit = infer_context_window_limit(model_id)
        if limit:
            pct = (context_tokens / limit) * 100.0
            limit_label = "1M" if limit == 1_048_576 else f"{limit // 1000}K"
            main = f"`{context_tokens:,}` / `{limit:,}` ({pct:.1f}% of {limit_label})"
        else:
            main = f"`{context_tokens:,}` tokens"
    else:
        main = "*(tunggu turn berikutnya untuk ukuran context)*"

    totals: list[str] = []
    if input_tokens is not None and input_tokens > 0:
        totals.append(f"{_fmt_compact(input_tokens)} in")
    if cache_read_tokens is not None and cache_read_tokens > 0:
        totals.append(f"{_fmt_compact(cache_read_tokens)} cached")
    if output_tokens is not None and output_tokens > 0:
        totals.append(f"{_fmt_compact(output_tokens)} out")
    return f"{main} · total sesi: {', '.join(totals)}" if totals else main
