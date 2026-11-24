"""Configuration and persistent per-channel state for the Antigravity Discord Gateway."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

VALID_MODES = ("auto", "accept-edits", "plan")
VALID_MEMORY_MODES = ("off", "readonly", "readwrite")


def _parse_int_set(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned:
            result.add(int(cleaned))
    return result


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class GatewayConfig:
    """Runtime configuration loaded from environment variables / .env."""

    discord_bot_token: str
    allowed_user_ids: set[int]
    channel_ids: set[int]
    allow_dm: bool = True
    guild_id: int | None = None
    require_mention: bool = False
    free_response_guild_ids: set[int] = field(default_factory=set)
    free_response_channel_ids: set[int] = field(default_factory=set)
    ignore_other_bot_mentions: bool = True
    mention_backfill_limit: int = 15
    agy_bin: str = "agy"
    default_workdir: Path = Path.cwd()
    default_mode: str = "auto"
    default_model: str | None = None
    sandbox: bool = False
    skip_permissions: bool = False
    timeout_seconds: int = 0
    state_file: Path = Path("state.json")
    stream_edit_interval: float = 1.2
    hermes_memory_mode: str = "readonly"
    hermes_soul_file: Path | None = None
    hermes_memory_file: Path | None = None
    hermes_user_file: Path | None = None
    agy_memory_file: Path | None = None
    agy_conversations_db: Path = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
    hermes_state_db: Path | None = None
    cross_channel_history_limit: int = 15

    def is_authorized(
        self,
        user_id: int,
        channel_id: int,
        is_dm: bool,
        *,
        parent_channel_id: int | None = None,
    ) -> bool:
        """Check whether a Discord user in a channel/DM is authorized."""
        if user_id not in self.allowed_user_ids:
            return False
        if is_dm:
            return self.allow_dm
        if not self.channel_ids:
            return True
        if channel_id in self.channel_ids:
            return True
        return parent_channel_id is not None and parent_channel_id in self.channel_ids

    def is_free_response_channel(
        self,
        *,
        is_dm: bool,
        guild_id: int | None = None,
        channel_id: int | None = None,
        parent_channel_id: int | None = None,
        in_other_bot_thread: bool = False,
    ) -> bool:
        """Return True if messages in this context do not require an explicit @mention."""
        if is_dm:
            return True
        if in_other_bot_thread:
            return False
        if not self.require_mention:
            return True
        if guild_id is not None and guild_id in self.free_response_guild_ids:
            return True
        if channel_id is not None and channel_id in self.free_response_channel_ids:
            return True
        if parent_channel_id is not None and parent_channel_id in self.free_response_channel_ids:
            return True
        return False

    def should_respond_to_message(
        self,
        *,
        is_dm: bool,
        self_mentioned: bool,
        other_bot_mentioned: bool,
        guild_id: int | None = None,
        channel_id: int | None = None,
        parent_channel_id: int | None = None,
        in_other_bot_thread: bool = False,
    ) -> bool:
        """Decide whether the bot should respond to a guild/DM message."""
        if is_dm:
            return self.allow_dm
        if self.ignore_other_bot_mentions and other_bot_mentioned and not self_mentioned:
            return False
        if self.is_free_response_channel(
            is_dm=False,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
            in_other_bot_thread=in_other_bot_thread,
        ):
            return True
        return self_mentioned


def load_config(env_path: Path | None = None) -> GatewayConfig:
    """Load and validate GatewayConfig from .env and process environment."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        load_dotenv(override=False)

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required. Run `python gateway.py setup` first.")

    allowed_users = _parse_int_set(os.getenv("DISCORD_ALLOWED_USER_IDS", ""))
    if not allowed_users:
        raise ValueError("DISCORD_ALLOWED_USER_IDS must contain at least one Discord user ID.")

    channel_ids = _parse_int_set(os.getenv("DISCORD_CHANNEL_IDS", ""))
    allow_dm = _parse_bool(os.getenv("DISCORD_ALLOW_DM"), default=True)

    guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_raw) if guild_raw else None

    require_mention = _parse_bool(os.getenv("DISCORD_REQUIRE_MENTION"), default=False)
    free_response_guild_ids = _parse_int_set(os.getenv("DISCORD_FREE_RESPONSE_GUILD_IDS", ""))
    free_response_channel_ids = _parse_int_set(os.getenv("DISCORD_FREE_RESPONSE_CHANNEL_IDS", ""))
    ignore_other_bot_mentions = _parse_bool(
        os.getenv("DISCORD_IGNORE_OTHER_BOT_MENTIONS"), default=True
    )
    mention_backfill_limit = max(
        0, int(os.getenv("DISCORD_MENTION_BACKFILL_LIMIT", "15").strip() or "15")
    )

    mode = os.getenv("AGY_MODE", "auto").strip() or "auto"
    if mode not in VALID_MODES:
        raise ValueError(f"AGY_MODE must be one of {VALID_MODES}, got {mode!r}")

    workdir = Path(os.getenv("AGY_WORKDIR", str(Path.cwd()))).expanduser().resolve()
    model = os.getenv("AGY_MODEL", "").strip() or None
    state_file = Path(os.getenv("GATEWAY_STATE_FILE", "state.json")).expanduser()

    memory_mode = os.getenv("HERMES_MEMORY_MODE", "readonly").strip().lower() or "readonly"
    if memory_mode not in VALID_MEMORY_MODES:
        raise ValueError(
            f"HERMES_MEMORY_MODE must be one of {VALID_MEMORY_MODES}, got {memory_mode!r}"
        )

    def _opt_path(env_key: str) -> Path | None:
        raw = os.getenv(env_key, "").strip()
        return Path(raw).expanduser() if raw else None

    default_agy_db = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
    agy_db = _opt_path("AGY_CONVERSATIONS_DB") or default_agy_db
    hermes_state_db = _opt_path("HERMES_STATE_DB")
    if hermes_state_db is None and memory_mode != "off":
        candidate = Path.home() / ".hermes" / "state.db"
        if candidate.is_file():
            hermes_state_db = candidate

    default_agy_memory = Path.home() / ".gemini" / "config" / "AGY_MEMORY.md"
    agy_memory_file = _opt_path("AGY_MEMORY_FILE")
    if agy_memory_file is None and default_agy_memory.is_file():
        agy_memory_file = default_agy_memory

    return GatewayConfig(
        discord_bot_token=token,
        allowed_user_ids=allowed_users,
        channel_ids=channel_ids,
        allow_dm=allow_dm,
        guild_id=guild_id,
        require_mention=require_mention,
        free_response_guild_ids=free_response_guild_ids,
        free_response_channel_ids=free_response_channel_ids,
        ignore_other_bot_mentions=ignore_other_bot_mentions,
        mention_backfill_limit=mention_backfill_limit,
        agy_bin=os.getenv("AGY_BIN", "agy").strip() or "agy",
        default_workdir=workdir,
        default_mode=mode,
        default_model=model,
        sandbox=_parse_bool(os.getenv("AGY_SANDBOX"), default=False),
        skip_permissions=_parse_bool(os.getenv("AGY_SKIP_PERMISSIONS"), default=False),
        timeout_seconds=int(os.getenv("AGY_TIMEOUT_SECONDS", "0").strip() or "0"),
        state_file=state_file,
        stream_edit_interval=max(0.5, float(os.getenv("STREAM_EDIT_INTERVAL_SECONDS", "1.2"))),
        hermes_memory_mode=memory_mode,
        hermes_soul_file=_opt_path("HERMES_SOUL_FILE"),
        hermes_memory_file=_opt_path("HERMES_MEMORY_FILE"),
        hermes_user_file=_opt_path("HERMES_USER_FILE"),
        agy_memory_file=agy_memory_file,
        agy_conversations_db=agy_db,
        hermes_state_db=hermes_state_db,
        cross_channel_history_limit=max(
            1, int(os.getenv("CROSS_CHANNEL_HISTORY_LIMIT", "15").strip() or "15")
        ),
    )


def build_hermes_context_block(config: GatewayConfig) -> str | None:
    """Read configured Hermes SOUL.md, USER.md, and MEMORY.md into a prompt prefix."""
    if config.hermes_memory_mode == "off":
        return None

    sections: list[str] = []
    sources = (
        ("SOUL.md (Persona & Tone)", config.hermes_soul_file),
        ("USER.md (User Profile)", config.hermes_user_file),
        ("MEMORY.md (Persistent Memory)", config.hermes_memory_file),
        ("AGY_MEMORY.md (Antigravity Persistent Memory)", config.agy_memory_file),
    )
    for title, path in sources:
        if path is None or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            sections.append(f"### {title} [{path}]\n{content}")

    if not sections:
        return None

    mode_note = (
        "Read-write mode: you may update USER.md or MEMORY.md using Hermes section delimiters (`§`) "
        "only when learning durable new facts or user preferences."
        if config.hermes_memory_mode == "readwrite"
        else "Read-only shared context from Hermes: adopt the persona and remember these facts, "
        "but do NOT modify these Hermes files."
    )
    joined = "\n\n".join(sections)
    return (
        "[Hermes Persona & Memory Context]\n"
        f"{mode_note}\n\n"
        f"{joined}\n"
        "[End Hermes Context]"
    )


def hermes_extra_dirs(config: GatewayConfig) -> list[Path]:
    """Return directories to grant via `--add-dir` when Hermes read-write mode is active."""
    if config.hermes_memory_mode != "readwrite":
        return []
    dirs: list[Path] = []
    seen: set[Path] = set()
    for path in (config.hermes_memory_file, config.hermes_user_file, config.hermes_soul_file):
        if path is not None and path.parent.is_dir():
            resolved = path.parent.resolve()
            if resolved not in seen:
                seen.add(resolved)
                dirs.append(resolved)
    return dirs


@dataclass
class ChannelState:
    """Persistent agent session settings bound to a Discord channel or DM."""

    conversation_id: str | None = None
    model: str | None = None
    mode: str | None = None
    workdir: str | None = None
    last_context_tokens: int | None = None
    last_input_tokens: int | None = None
    last_cache_read_tokens: int | None = None
    last_output_tokens: int | None = None


class StateStore:
    """JSON-backed persistence for per-channel conversation and override state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._channels: dict[str, ChannelState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        channels = raw.get("channels", {})
        if isinstance(channels, dict):
            for key, val in channels.items():
                if isinstance(val, dict):
                    self._channels[str(key)] = ChannelState(
                        conversation_id=val.get("conversation_id"),
                        model=val.get("model"),
                        mode=val.get("mode"),
                        workdir=val.get("workdir"),
                        last_context_tokens=val.get("last_context_tokens"),
                        last_input_tokens=val.get("last_input_tokens"),
                        last_cache_read_tokens=val.get("last_cache_read_tokens"),
                        last_output_tokens=val.get("last_output_tokens"),
                    )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"channels": {k: asdict(v) for k, v in self._channels.items()}}
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def get(self, channel_id: int) -> ChannelState:
        """Return the ChannelState for channel_id (creating an empty one if absent)."""
        key = str(channel_id)
        if key not in self._channels:
            self._channels[key] = ChannelState()
        return self._channels[key]

    def update(
        self,
        channel_id: int,
        *,
        conversation_id: str | None | object = ...,
        model: str | None | object = ...,
        mode: str | None | object = ...,
        workdir: str | None | object = ...,
        last_context_tokens: int | None | object = ...,
        last_input_tokens: int | None | object = ...,
        last_cache_read_tokens: int | None | object = ...,
        last_output_tokens: int | None | object = ...,
    ) -> ChannelState:
        """Update specific fields of a channel's state and persist to disk."""
        state = self.get(channel_id)
        if conversation_id is not ...:
            state.conversation_id = conversation_id  # type: ignore[assignment]
        if model is not ...:
            state.model = model  # type: ignore[assignment]
        if mode is not ...:
            state.mode = mode  # type: ignore[assignment]
        if workdir is not ...:
            state.workdir = workdir  # type: ignore[assignment]
        if last_context_tokens is not ...:
            state.last_context_tokens = last_context_tokens  # type: ignore[assignment]
        if last_input_tokens is not ...:
            state.last_input_tokens = last_input_tokens  # type: ignore[assignment]
        if last_cache_read_tokens is not ...:
            state.last_cache_read_tokens = last_cache_read_tokens  # type: ignore[assignment]
        if last_output_tokens is not ...:
            state.last_output_tokens = last_output_tokens  # type: ignore[assignment]
        self._save()
        return state
