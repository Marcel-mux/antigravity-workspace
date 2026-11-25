"""Unit tests for Antigravity Discord Gateway modules."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agy_gateway.bot import AntigravityBot
from agy_gateway.config import (
    GatewayConfig,
    StateStore,
    build_hermes_context_block,
    hermes_extra_dirs,
    load_config,
)
from agy_gateway.formatting import (
    extract_channel_mentions,
    format_channel_backfill_block,
    format_referenced_channel_block,
    format_streaming_preview,
    format_usage_report,
    parse_stream_event,
    split_message,
    strip_bot_mention,
)
from agy_gateway.runner import build_agy_command, parse_models_output
from agy_gateway.search import (
    format_search_results,
    search_antigravity_sessions,
    search_hermes_sessions,
)


def test_parse_stream_events() -> None:
    init_line = '{"event":"init","conversation_id":"conv-123","init":{"cwd":"/tmp"}}'
    ev1 = parse_stream_event(init_line)
    assert ev1 is not None
    assert ev1.event_type == "init"
    assert ev1.conversation_id == "conv-123"

    delta_line = (
        '{"event":"step_update","step_update":{"conversation_id":"conv-123",'
        '"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"Halo"}}'
    )
    ev2 = parse_stream_event(delta_line)
    assert ev2 is not None
    assert ev2.text_delta == "Halo"
    assert ev2.step_type == "agent_response"

    result_line = (
        '{"event":"result","result":{"conversation_id":"conv-123",'
        '"status":"SUCCESS","response":"Halo dunia","duration_seconds":1.5}}'
    )
    ev3 = parse_stream_event(result_line)
    assert ev3 is not None
    assert ev3.final_response == "Halo dunia"
    assert ev3.status == "SUCCESS"
    assert parse_stream_event("not json") is None


def test_split_message_preserves_code_fences() -> None:
    body = "```python\n" + "\n".join(f"print({i})" for i in range(400)) + "\n```"
    chunks = split_message(body, max_len=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500
        assert chunk.count("```") % 2 == 0


def test_format_streaming_preview_closes_open_fence() -> None:
    preview = format_streaming_preview("```bash\nls -la", "⏳ Writing...")
    assert preview.count("```") == 2
    assert preview.endswith("⏳ Writing...")


def test_build_agy_command() -> None:
    cmd = build_agy_command(
        "agy",
        "cek status",
        conversation_id="conv-1",
        mode="plan",
        model="gemini-4-hydrogen-high",
        sandbox=True,
        skip_permissions=True,
        timeout_seconds=120,
    )
    assert cmd == [
        "agy",
        "-p",
        "cek status",
        "--output-format",
        "stream-json",
        "--mode",
        "plan",
        "--conversation",
        "conv-1",
        "--model",
        "gemini-4-hydrogen-high",
        "--sandbox",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "120s",
    ]


def test_state_store_persistence(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = StateStore(state_file)
    store.update(111, conversation_id="abc", mode="plan", model="gemini-4-neon-high")

    reloaded = StateStore(state_file)
    ch = reloaded.get(111)
    assert ch.conversation_id == "abc"
    assert ch.mode == "plan"
    assert ch.model == "gemini-4-neon-high"


def test_config_authorization_and_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_BOT_TOKEN=tok123\n"
        "DISCORD_ALLOWED_USER_IDS=42,99\n"
        "DISCORD_CHANNEL_IDS=555\n"
        "DISCORD_ALLOW_DM=false\n"
        "AGY_MODE=auto\n",
        encoding="utf-8",
    )
    for key in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_CHANNEL_IDS",
        "DISCORD_ALLOW_DM",
        "AGY_MODE",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(env_file)
    assert isinstance(cfg, GatewayConfig)
    assert cfg.is_authorized(42, 555, is_dm=False) is True
    assert cfg.is_authorized(77, 555, is_dm=False) is False
    assert cfg.is_authorized(42, 999, is_dm=False) is False
    assert cfg.is_authorized(42, 123, is_dm=True) is False


def test_config_allows_all_channels_when_channel_ids_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_BOT_TOKEN=tok123\n"
        "DISCORD_ALLOWED_USER_IDS=42\n"
        "DISCORD_CHANNEL_IDS=\n"
        "DISCORD_ALLOW_DM=false\n",
        encoding="utf-8",
    )
    for key in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_CHANNEL_IDS",
        "DISCORD_ALLOW_DM",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(env_file)
    assert cfg.is_authorized(42, 111111, is_dm=False) is True
    assert cfg.is_authorized(42, 222222, is_dm=False) is True
    assert cfg.is_authorized(99, 111111, is_dm=False) is False
    assert cfg.is_authorized(42, 111111, is_dm=True) is False


def test_hermes_soul_and_memory_context(tmp_path: Path) -> None:
    soul = tmp_path / "SOUL.md"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    mem = mem_dir / "MEMORY.md"
    user = mem_dir / "USER.md"

    soul.write_text("Santai dan to the point.", encoding="utf-8")
    mem.write_text("Server utama pakai Ubuntu.", encoding="utf-8")
    user.write_text("Panggil bro.", encoding="utf-8")

    cfg_ro = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={1},
        channel_ids=set(),
        hermes_memory_mode="readonly",
        hermes_soul_file=soul,
        hermes_memory_file=mem,
        hermes_user_file=user,
    )
    block_ro = build_hermes_context_block(cfg_ro)
    assert block_ro is not None
    assert "Santai dan to the point." in block_ro
    assert "Panggil bro." in block_ro
    assert "Server utama pakai Ubuntu." in block_ro
    assert "Read-only" in block_ro
    assert hermes_extra_dirs(cfg_ro) == []

    cfg_rw = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={1},
        channel_ids=set(),
        hermes_memory_mode="readwrite",
        hermes_soul_file=soul,
        hermes_memory_file=mem,
        hermes_user_file=user,
    )
    block_rw = build_hermes_context_block(cfg_rw)
    assert block_rw is not None
    assert "Read-write mode" in block_rw
    assert mem_dir.resolve() in hermes_extra_dirs(cfg_rw)

    cfg_off = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={1},
        channel_ids=set(),
        hermes_memory_mode="off",
        hermes_soul_file=soul,
    )
    assert build_hermes_context_block(cfg_off) is None


def test_cross_channel_mentions_and_formatting() -> None:
    text = "Cek diskusi di <#111> dan <#222> serta <#111> dan <#999>"
    ids = extract_channel_mentions(text, exclude_channel_id=999)
    assert ids == [111, 222]

    block = format_referenced_channel_block(
        "riset-api",
        111,
        [("User", "Pakai FastAPI aja"), ("Antigravity", "Oke siap pakai FastAPI.")],
        conversation_id="conv-xyz",
    )
    assert "#riset-api" in block
    assert "agy_conversation_id=conv-xyz" in block
    assert "Pakai FastAPI aja" in block


def test_session_search_antigravity_and_hermes(tmp_path: Path) -> None:
    agy_db = tmp_path / "conversation_summaries.db"
    con = sqlite3.connect(agy_db)
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT, last_modified_time TEXT)"
    )
    con.execute(
        "CREATE VIRTUAL TABLE conversations_fts USING fts5("
        "conversation_id UNINDEXED, last_modified_time UNINDEXED, title, user_body, agent_body)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
        ("agy-1", "Discord Gateway Setup", "Setup bot discord", "2026-11-20T00:00:00"),
    )
    con.execute(
        "INSERT INTO conversations_fts VALUES (?, ?, ?, ?, ?)",
        (
            "agy-1",
            "2026-11-20T00:00:00",
            "Discord Gateway Setup",
            "bikin bot discord pakai agy",
            "siap gw buatin",
        ),
    )
    con.commit()
    con.close()

    hermes_db = tmp_path / "state.db"
    hcon = sqlite3.connect(hermes_db)
    hcon.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT)")
    hcon.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    hcon.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id')"
    )
    hcon.execute("INSERT INTO sessions VALUES ('hermes-1', 'discord', 'Deploy VPS')")
    hcon.execute(
        "INSERT INTO messages VALUES (1, 'hermes-1', 'user', 'cara deploy docker di ubuntu', 1763568000.0)"
    )
    hcon.execute("INSERT INTO messages_fts(rowid, content) VALUES (1, 'cara deploy docker di ubuntu')")
    hcon.commit()
    hcon.close()

    agy_hits = search_antigravity_sessions(agy_db, "discord agy")
    assert len(agy_hits) == 1
    assert agy_hits[0].session_id == "agy-1"
    assert agy_hits[0].source == "antigravity"

    hermes_hits = search_hermes_sessions(hermes_db, "docker ubuntu")
    assert len(hermes_hits) == 1
    assert hermes_hits[0].session_id == "hermes-1"
    assert hermes_hits[0].source == "hermes"

    rendered = format_search_results("deploy", agy_hits + hermes_hits)
    assert "[AGY]" in rendered
    assert "[HERMES]" in rendered
    assert "/resume <ID>" in rendered


def test_parse_models_output_strips_spinner_and_parses_pairs() -> None:
    raw = (
        "\r⠋ Fetching available models...\r⠙ Fetching available models...\n"
        "gemini-4-neon-high              Gemini 4 Neon (High)\n"
        "gemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)\n"
        "claude-opus-5-5-medium\t\tClaude Opus 5.5 (Medium)\n"
    )
    parsed = parse_models_output(raw)
    assert parsed == [
        ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
        ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
        ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
    ]


def test_format_usage_report() -> None:
    payload = {
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon, Gemini 4 Carbon, Gemini 4 Argon, Gemini 4 Hydrogen, Orchestrator (Neon), Orchestrator (Carbon), Orchestrator (Argon), Gemini 3.8 Flash",
                        "buckets": [
                            {
                                "name": "Weekly Limit Remaining",
                                "remaining_fraction": 0.95,
                                "reset_time": "2026-11-26T13:59:39Z",
                            },
                            {
                                "name": "Five Hour Limit Remaining",
                                "remaining_fraction": 0.873,
                                "reset_time": "2026-11-19T18:59:39Z",
                            },
                        ],
                    }
                ]
            },
        }
    }
    report = format_usage_report(payload)
    assert "Antigravity Quota & Usage" in report
    assert "Gemini Models" in report
    assert "Gemini 4 Argon" in report
    assert "95.0%" in report
    assert "87.3%" in report
    assert "Weekly Limit Remaining" in report


def test_mention_policy_and_multi_server_gating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_BOT_TOKEN=tok123\n"
        "DISCORD_ALLOWED_USER_IDS=42\n"
        "DISCORD_CHANNEL_IDS=\n"
        "DISCORD_ALLOW_DM=true\n"
        "DISCORD_REQUIRE_MENTION=true\n"
        "DISCORD_FREE_RESPONSE_GUILD_IDS=1001\n"
        "DISCORD_FREE_RESPONSE_CHANNEL_IDS=2002\n"
        "DISCORD_IGNORE_OTHER_BOT_MENTIONS=true\n"
        "DISCORD_MENTION_BACKFILL_LIMIT=20\n",
        encoding="utf-8",
    )
    for key in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_CHANNEL_IDS",
        "DISCORD_ALLOW_DM",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_GUILD_IDS",
        "DISCORD_FREE_RESPONSE_CHANNEL_IDS",
        "DISCORD_IGNORE_OTHER_BOT_MENTIONS",
        "DISCORD_MENTION_BACKFILL_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(env_file)
    assert cfg.require_mention is True
    assert cfg.free_response_guild_ids == {1001}
    assert cfg.free_response_channel_ids == {2002}
    assert cfg.ignore_other_bot_mentions is True
    assert cfg.mention_backfill_limit == 20

    # Primary server (1001): free-response for normal chat, silent if only @Hermes is tagged
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=False, other_bot_mentioned=False, guild_id=1001, channel_id=10
    ) is True
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=False, other_bot_mentioned=True, guild_id=1001, channel_id=10
    ) is False
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=True, other_bot_mentioned=True, guild_id=1001, channel_id=10
    ) is True

    # Other bot's thread inside primary server: mention-only
    assert cfg.should_respond_to_message(
        is_dm=False,
        self_mentioned=False,
        other_bot_mentioned=False,
        guild_id=1001,
        channel_id=99,
        parent_channel_id=10,
        in_other_bot_thread=True,
    ) is False
    assert cfg.should_respond_to_message(
        is_dm=False,
        self_mentioned=True,
        other_bot_mentioned=False,
        guild_id=1001,
        channel_id=99,
        parent_channel_id=10,
        in_other_bot_thread=True,
    ) is True

    # Secondary server (9999 = Hermes server): mention-only unless channel 2002
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=False, other_bot_mentioned=False, guild_id=9999, channel_id=50
    ) is False
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=True, other_bot_mentioned=False, guild_id=9999, channel_id=50
    ) is True
    assert cfg.should_respond_to_message(
        is_dm=False, self_mentioned=False, other_bot_mentioned=False, guild_id=9999, channel_id=2002
    ) is True

    # DM always allowed when allow_dm=True
    assert cfg.should_respond_to_message(
        is_dm=True, self_mentioned=False, other_bot_mentioned=False
    ) is True

    # Thread inherits parent channel authorization when channel_ids whitelist is set
    cfg_restricted = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={42},
        channel_ids={555},
    )
    assert cfg_restricted.is_authorized(42, 777, is_dm=False, parent_channel_id=555) is True
    assert cfg_restricted.is_authorized(42, 777, is_dm=False, parent_channel_id=888) is False


def test_strip_bot_mention_and_backfill_formatting() -> None:
    assert strip_bot_mention("<@12345> cek ini dong", 12345) == "cek ini dong"
    assert strip_bot_mention("<@!12345>   !status", 12345) == "!status"
    code_msg = "<@12345>\n```python\n    def f():\n        return 1\n```"
    assert strip_bot_mention(code_msg, 12345) == "```python\n    def f():\n        return 1\n```"

    # Prioritizes newest messages when over max_chars, keeps chronological order
    msgs = [
        ("ndrong", "pesan lama " + "a" * 400),
        ("Mux-Hermes (bot)", "saran hermes terbaru"),
        ("ndrong", "coba review ini"),
    ]
    block = format_channel_backfill_block(
        "planning",
        msgs,
        replied_message=("Mux-Hermes (bot)", "arsitektur pakai sqlite aja dulu"),
        max_chars=180,
    )
    assert block is not None
    assert "saran hermes terbaru" in block
    assert "coba review ini" in block
    assert "pesan lama" not in block
    assert block.index("saran hermes terbaru") < block.index("coba review ini")
    assert "[Message Replied To]" in block
    assert "arsitektur pakai sqlite aja dulu" in block


def test_bot_on_message_gating_and_backfill_stops_at_self(tmp_path: Path) -> None:
    cfg = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={42},
        channel_ids=set(),
        require_mention=True,
        free_response_guild_ids={1001},
        ignore_other_bot_mentions=True,
        mention_backfill_limit=10,
        state_file=tmp_path / "state.json",
    )
    bot = AntigravityBot(cfg)
    bot_user = SimpleNamespace(id=9001, bot=True, display_name="Mux-Antigravity")
    hermes_user = SimpleNamespace(id=9002, bot=True, display_name="Mux-Hermes")
    human_user = SimpleNamespace(id=42, bot=False, display_name="ndrong")
    bot._connection.user = bot_user  # type: ignore[assignment]

    executed: list[str] = []
    replied: list[str] = []

    async def fake_execute(message: object, prompt: str, workdir: Path) -> None:
        executed.append(prompt)

    bot._execute_and_stream = fake_execute  # type: ignore[method-assign]

    class FakeChannel:
        def __init__(self, cid: int, guild_id: int, history_msgs: list[object]) -> None:
            self.id = cid
            self.name = "planning"
            self.guild = SimpleNamespace(id=guild_id)
            self.parent_id = None
            self._history_msgs = history_msgs

        async def history(self, *, limit: int = 10, before: object = None):
            for m in self._history_msgs[:limit]:
                yield m

    async def _run() -> None:
        # History is newest-first as returned by discord.py
        hist_with_hermes = [
            SimpleNamespace(author=hermes_user, content="Menurut gw pakai Postgres."),
            SimpleNamespace(author=human_user, content="<@9002> menurut lo gimana?"),
            SimpleNamespace(author=bot_user, content="Balasan Antigravity sebelumnya."),
            SimpleNamespace(author=human_user, content="Pesan jauh sebelum itu."),
        ]
        ch_agy = FakeChannel(10, 1001, hist_with_hermes)
        ch_hermes = FakeChannel(20, 9999, hist_with_hermes)

        async def _fake_reply(text: str, **kwargs: object) -> None:
            replied.append(text)

        # 1) Primary server, tagging @Hermes only -> Antigravity stays silent
        msg_to_hermes = SimpleNamespace(
            id=1,
            author=human_user,
            channel=ch_agy,
            guild=ch_agy.guild,
            content="<@9002> menurut lo gimana?",
            mentions=[hermes_user],
            attachments=[],
            reference=None,
            reply=_fake_reply,
        )
        await bot.on_message(msg_to_hermes)  # type: ignore[arg-type]
        assert executed == []

        # 2) Primary server, follow-up to Antigravity -> auto-backfills Hermes's inline reply, stops at self
        msg_followup = SimpleNamespace(
            id=2,
            author=human_user,
            channel=ch_agy,
            guild=ch_agy.guild,
            content="Oke revisi sesuai saran Hermes barusan",
            mentions=[],
            attachments=[],
            reference=None,
            reply=_fake_reply,
        )
        await bot.on_message(msg_followup)  # type: ignore[arg-type]
        assert len(executed) == 1
        assert "Mux-Hermes (bot)" in executed[0]
        assert "Menurut gw pakai Postgres." in executed[0]
        assert "Pesan jauh sebelum itu." not in executed[0]

        # 3) Hermes server, unmentioned chat & unmentioned !status -> ignored
        msg_unmentioned = SimpleNamespace(
            id=3,
            author=human_user,
            channel=ch_hermes,
            guild=ch_hermes.guild,
            content="!status",
            mentions=[],
            attachments=[],
            reference=None,
            reply=_fake_reply,
        )
        await bot.on_message(msg_unmentioned)  # type: ignore[arg-type]
        assert len(executed) == 1
        assert replied == []

        # 4) Hermes server, @Antigravity !status -> strips mention & runs prefix command
        msg_mentioned_cmd = SimpleNamespace(
            id=4,
            author=human_user,
            channel=ch_hermes,
            guild=ch_hermes.guild,
            content="<@9001> !status",
            mentions=[bot_user],
            attachments=[],
            reference=None,
            reply=_fake_reply,
        )
        await bot.on_message(msg_mentioned_cmd)  # type: ignore[arg-type]
        assert len(replied) == 1
        assert "mention-only" in replied[0]

        # 5) Hermes server, @Antigravity prompt -> strips mention & includes backfill
        msg_mentioned_prompt = SimpleNamespace(
            id=5,
            author=human_user,
            channel=ch_hermes,
            guild=ch_hermes.guild,
            content="<@9001> bandingin saran Hermes dong",
            mentions=[bot_user],
            attachments=[],
            reference=None,
            reply=_fake_reply,
        )
        await bot.on_message(msg_mentioned_prompt)  # type: ignore[arg-type]
        assert len(executed) == 2
        assert "<@9001>" not in executed[1]
        assert "bandingin saran Hermes dong" in executed[1]
        assert "Mux-Hermes (bot)" in executed[1]

    asyncio.run(_run())


def test_stream_usage_parsing_and_status_context_display(tmp_path: Path) -> None:
    from agy_gateway.formatting import format_context_usage, parse_stream_event

    line = (
        '{"event":"result","result":{"conversation_id":"conv-1","status":"SUCCESS",'
        '"response":"ok","duration_seconds":1.2,'
        '"usage":{"input_tokens":12110,"output_tokens":193,"cache_read_tokens":7719,"total_tokens":12303}}}'
    )
    ev = parse_stream_event(line)
    assert ev is not None
    assert ev.usage == {
        "input_tokens": 12110,
        "output_tokens": 193,
        "cache_read_tokens": 7719,
        "total_tokens": 12303,
    }

    formatted_gemini = format_context_usage(
        58420, 12110, 7719, 193, model_id="gemini-4-hydrogen-medium"
    )
    assert "`58,420` / `1,048,576`" in formatted_gemini
    assert "5.6% of 1M" in formatted_gemini
    assert "total sesi: 12.1K in, 7.7K cached, 193 out" in formatted_gemini

    formatted_claude = format_context_usage(50000, 50000, 0, 200, model_id="claude-sonnet-5-5-medium")
    assert "`50,000` / `200,000`" in formatted_claude
    assert "25.0% of 200K" in formatted_claude

    store = StateStore(tmp_path / "state.json")
    store.update(
        123,
        conversation_id="conv-1",
        model="gemini-4-hydrogen-medium",
        last_context_tokens=58420,
        last_input_tokens=12110,
        last_cache_read_tokens=7719,
        last_output_tokens=193,
    )
    reloaded = StateStore(tmp_path / "state.json").get(123)
    assert reloaded.last_context_tokens == 58420
    assert reloaded.last_input_tokens == 12110
    assert reloaded.last_cache_read_tokens == 7719
    assert reloaded.last_output_tokens == 193


def test_mcp_memory_server_and_session_detail(tmp_path: Path) -> None:
    import sqlite3

    from agy_gateway.mcp_memory import handle_rpc_request

    mem_file = tmp_path / "AGY_MEMORY.md"
    agy_db = tmp_path / "conversation_summaries.db"
    hermes_db = tmp_path / "state.db"

    con_a = sqlite3.connect(agy_db)
    con_a.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT, step_count INT, last_modified_time TEXT, workspace_uris TEXT)"
    )
    con_a.execute(
        "CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, chunk_index UNINDEXED, last_modified_time UNINDEXED, title, user_body, agent_body)"
    )
    con_a.execute(
        "INSERT INTO conversation_summaries VALUES ('conv-abc', 'CV & Portfolio', 'Created PDF CV', 4, '2026-11-23T18:50:00', '/root/projects/portfolio-next')"
    )
    con_a.execute(
        "INSERT INTO conversations_fts VALUES ('conv-abc', 0, '2026-11-23T18:50:00', 'CV & Portfolio', 'buatin cv pdf', 'CV PDF sudah dibuat dan di-push ke github')"
    )
    con_a.commit()
    con_a.close()

    con_h = sqlite3.connect(hermes_db)
    con_h.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL, cwd TEXT)")
    con_h.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    con_h.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
    con_h.execute("INSERT INTO sessions VALUES ('hs-1', 'telegram', 'Server Ops', 1795400000.0, '/root')")
    con_h.execute("INSERT INTO messages VALUES (1, 'hs-1', 'user', 'cek ram muxoracle', 1795400010.0)")
    con_h.execute("INSERT INTO messages_fts(rowid, content) VALUES (1, 'cek ram muxoracle')")
    con_h.commit()
    con_h.close()

    init_resp = handle_rpc_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        memory_path=mem_file,
        agy_db=agy_db,
        hermes_db=hermes_db,
    )
    assert init_resp is not None and init_resp["result"]["serverInfo"]["name"] == "agy-memory-search"

    # Notification yields None
    assert (
        handle_rpc_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, memory_path=mem_file)
        is None
    )

    tools_resp = handle_rpc_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools_resp is not None
    tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert {"memory_read", "memory_save", "memory_remove", "session_search", "session_get_detail"} <= tool_names

    save_resp = handle_rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "memory_save", "arguments": {"note": "Always run pytest before restart", "section": "Ops"}},
        },
        memory_path=mem_file,
    )
    assert save_resp is not None and "Saved" in save_resp["result"]["content"][0]["text"]

    read_resp = handle_rpc_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory_read", "arguments": {}}},
        memory_path=mem_file,
    )
    assert read_resp is not None and "Always run pytest before restart" in read_resp["result"]["content"][0]["text"]

    search_resp = handle_rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "session_search", "arguments": {"query": "github"}},
        },
        memory_path=mem_file,
        agy_db=agy_db,
        hermes_db=hermes_db,
    )
    assert search_resp is not None and "conv-abc" in search_resp["result"]["content"][0]["text"]

    detail_resp = handle_rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "session_get_detail", "arguments": {"session_id": "hs-1", "source": "hermes"}},
        },
        memory_path=mem_file,
        agy_db=agy_db,
        hermes_db=hermes_db,
    )
    assert detail_resp is not None and "cek ram muxoracle" in detail_resp["result"]["content"][0]["text"]

    rm_resp = handle_rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "memory_remove", "arguments": {"match": "pytest"}},
        },
        memory_path=mem_file,
    )
    assert rm_resp is not None and "Removed 1" in rm_resp["result"]["content"][0]["text"]


def test_compact_command_handoff_and_agy_memory_injection(tmp_path: Path) -> None:
    from agy_gateway.runner import AgyRunResult

    agy_mem = tmp_path / "AGY_MEMORY.md"
    agy_mem.write_text("- [2026-11-23] Use pytest before deploy", encoding="utf-8")

    cfg = GatewayConfig(
        discord_bot_token="tok",
        allowed_user_ids={111},
        channel_ids=set(),
        default_workdir=tmp_path,
        state_file=tmp_path / "state.json",
        hermes_memory_mode="readonly",
        agy_memory_file=agy_mem,
    )
    ctx_block = build_hermes_context_block(cfg)
    assert ctx_block is not None and "AGY_MEMORY.md" in ctx_block and "Use pytest before deploy" in ctx_block

    calls: list[tuple[str | None, str, object]] = []

    class _FakeRunner:
        def is_running(self, _cid: int) -> bool:
            return False

        async def run_prompt(self, _cid: int, prompt: str, **kwargs: object) -> AgyRunResult:
            conv = kwargs.get("conversation_id")
            calls.append((conv if isinstance(conv, str) else None, prompt, kwargs.get("model")))
            if conv == "old-conv-1":
                return AgyRunResult(
                    response="Goal: setup MCP.\nNext: restart service.",
                    conversation_id="old-conv-1",
                    context_tokens=150000,
                )
            return AgyRunResult(
                response="Siap lanjut.",
                conversation_id="new-conv-2",
                context_tokens=12000,
                usage={"input_tokens": 12000, "cache_read_tokens": 0, "output_tokens": 12},
            )

    bot = AntigravityBot(cfg)
    bot.runner = _FakeRunner()  # type: ignore[assignment]
    bot.state_store.update(
        999,
        conversation_id="old-conv-1",
        model="gemini-4-neon-medium",
        last_context_tokens=150000,
    )

    out = asyncio.run(bot._cmd_compact(999, focus="MCP setup"))
    assert "old-conv-1" in out and "new-conv-2" in out
    assert "150,000" in out and "12,000" in out
    assert len(calls) == 2
    assert calls[0][0] == "old-conv-1" and "MCP setup" in calls[0][1]
    assert calls[0][2] == "gemini-4-hydrogen-low"
    assert calls[1][0] is None and "Goal: setup MCP." in calls[1][1] and "AGY_MEMORY.md" in calls[1][1]
    assert calls[1][2] == "gemini-4-hydrogen-low"
    assert bot.state_store.get(999).conversation_id == "new-conv-2"
    assert bot.state_store.get(999).model == "gemini-4-neon-medium"
    assert bot.state_store.get(999).last_context_tokens == 12000


def test_sync_hermes_skills_adds_custom_and_prunes_stale(tmp_path: Path) -> None:
    from agy_gateway.skill_sync import sync_hermes_skills

    hermes_dir = tmp_path / "hermes_skills"
    agy_dir = tmp_path / "agy_skills"
    hermes_dir.mkdir()
    agy_dir.mkdir()

    # Manifest marks 'airtable' and 'pdf' as bundled.
    (hermes_dir / ".bundled_manifest").write_text("airtable:111\npdf:222\nhermes-agent:333\n")

    # Bundled allowlisted -> synced; bundled non-allowlisted -> skipped; excluded -> skipped.
    (hermes_dir / "productivity" / "pdf").mkdir(parents=True)
    (hermes_dir / "productivity" / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n")
    (hermes_dir / "productivity" / "airtable").mkdir(parents=True)
    (hermes_dir / "productivity" / "airtable" / "SKILL.md").write_text("---\nname: airtable\n---\n")
    (hermes_dir / "apple" / "apple-notes").mkdir(parents=True)
    (hermes_dir / "apple" / "apple-notes" / "SKILL.md").write_text("---\nname: apple-notes\n---\n")
    (hermes_dir / "autonomous-ai-agents" / "hermes-agent").mkdir(parents=True)
    (hermes_dir / "autonomous-ai-agents" / "hermes-agent" / "SKILL.md").write_text("---\nname: hermes-agent\n---\n")
    (hermes_dir / "security" / "godmode").mkdir(parents=True)
    (hermes_dir / "security" / "godmode" / "SKILL.md").write_text("---\nname: godmode\n---\n")

    # Custom skill added by Hermes -> automatically synced!
    (hermes_dir / "devops" / "new-custom-ops").mkdir(parents=True)
    (hermes_dir / "devops" / "new-custom-ops" / "SKILL.md").write_text("---\nname: new-custom-ops\n---\n")

    # Pre-existing real Agy-owned skill -> never touched; dead Hermes symlink -> pruned.
    (agy_dir / "agy-native-skill").mkdir()
    (agy_dir / "agy-native-skill" / "SKILL.md").write_text("native")
    (agy_dir / "removed-hermes-skill").symlink_to(hermes_dir / "devops" / "gone")

    res = sync_hermes_skills(hermes_dir, agy_dir)
    assert set(res.added) == {"new-custom-ops", "pdf"}
    assert res.removed == ["removed-hermes-skill"]
    assert res.total_linked == 2
    assert (agy_dir / "new-custom-ops").is_symlink()
    assert (agy_dir / "pdf").is_symlink()
    assert not (agy_dir / "airtable").exists()
    assert not (agy_dir / "apple-notes").exists()
    assert not (agy_dir / "hermes-agent").exists()
    assert not (agy_dir / "godmode").exists()
    assert (agy_dir / "agy-native-skill" / "SKILL.md").is_file()


def test_curator_check_updates_state_ledger_and_flags_issues(tmp_path: Path) -> None:
    from agy_gateway.curator import format_curator_report, run_curator_check

    hermes_dir = tmp_path / "hermes_skills"
    agy_dir = tmp_path / "agy_skills"
    ws_dir = tmp_path / "ws_skills"
    state_file = tmp_path / ".curator_state.json"
    ledger_file = tmp_path / ".curator_ledger.jsonl"

    (hermes_dir / "devops" / "ops-check").mkdir(parents=True)
    (hermes_dir / "devops" / "ops-check" / "SKILL.md").write_text(
        "---\nname: ops-check\ndescription: Check VPS services.\n---\n# Ops\n"
    )
    (agy_dir / "custom-agy").mkdir(parents=True)
    (agy_dir / "custom-agy" / "SKILL.md").write_text(
        "---\nname: custom-agy\ndescription: Valid local skill.\n---\n# Body\n"
    )
    (ws_dir / "broken-ws").mkdir(parents=True)
    (ws_dir / "broken-ws" / "SKILL.md").write_text("# Missing frontmatter\n")

    rep1 = run_curator_check(
        hermes_skills_dir=hermes_dir,
        agy_skills_dir=agy_dir,
        workspace_skills_dir=ws_dir,
        state_file=state_file,
        ledger_file=ledger_file,
    )
    assert rep1.run_count == 1
    assert rep1.sync_result.added == ["ops-check"]
    assert len(rep1.skills) == 3
    assert [s.name for s in rep1.invalid_skills] == ["broken-ws"]
    assert "missing YAML frontmatter" in rep1.invalid_skills[0].issues

    text = format_curator_report(rep1)
    assert "Run #1" in text and "broken-ws" in text
    assert state_file.is_file() and ledger_file.is_file()

    # Second run without changes should append 0 new added/updated events (only invalid if present).
    (ws_dir / "broken-ws" / "SKILL.md").write_text(
        "---\nname: broken-ws\ndescription: Fixed now.\n---\n# Fixed\n"
    )
    rep2 = run_curator_check(
        hermes_skills_dir=hermes_dir,
        agy_skills_dir=agy_dir,
        workspace_skills_dir=ws_dir,
        state_file=state_file,
        ledger_file=ledger_file,
    )
    assert rep2.run_count == 2
    assert rep2.invalid_skills == []
    assert rep2.ledger_events == 1  # updated hash for broken-ws -> fixed


def test_context_warning_threshold_and_footer() -> None:
    from agy_gateway.formatting import format_context_warning

    # Below threshold -> silent
    assert format_context_warning(120_000, model_id="gemini-4-hydrogen-medium") is None
    assert format_context_warning(200_000, model_id="gemini-4-hydrogen-medium", threshold=0) is None

    # Above 150K on 1M Gemini -> emits warning
    warn_gemini = format_context_warning(263_746, model_id="gemini-4-neon-medium")
    assert warn_gemini is not None
    assert "263,746" in warn_gemini and "25.2% of 1M" in warn_gemini and "/compact" in warn_gemini

    # Claude 200K window warns at 70% (140K) even when threshold=150K
    assert format_context_warning(130_000, model_id="claude-sonnet-5-5-medium") is None
    warn_claude = format_context_warning(145_000, model_id="claude-sonnet-5-5-medium")
    assert warn_claude is not None and "72.5% of 200K" in warn_claude
