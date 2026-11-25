"""Discord bot client bridging dedicated channels and DMs to the Antigravity CLI."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

import discord
from discord import app_commands

from agy_gateway.config import (
    VALID_MODES,
    GatewayConfig,
    StateStore,
    build_hermes_context_block,
    hermes_extra_dirs,
)
from agy_gateway.formatting import (
    extract_channel_mentions,
    format_channel_backfill_block,
    format_context_usage,
    format_context_warning,
    format_referenced_channel_block,
    format_streaming_preview,
    format_usage_report,
    split_message,
    strip_bot_mention,
)
from agy_gateway.curator import format_curator_report, run_curator_check
from agy_gateway.runner import AgyRunner
from agy_gateway.skill_sync import sync_hermes_skills
from agy_gateway.search import (
    SessionHit,
    format_search_results,
    search_antigravity_sessions,
    search_hermes_sessions,
)


def _is_dm_channel(channel: discord.abc.Messageable) -> bool:
    return isinstance(channel, discord.DMChannel)


def _sanitize_filename(name: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in name]
    return "".join(keep) or "attachment"


class ModelSelect(discord.ui.Select["ModelSelectView"]):
    """Interactive Discord dropdown for choosing the active channel model."""

    def __init__(
        self,
        bot: "AntigravityBot",
        channel_id: int,
        models: list[tuple[str, str]],
        current_model: str | None,
    ) -> None:
        options = [
            discord.SelectOption(
                label="Default (sesuai config)",
                value="__default__",
                description="Reset model channel ke default",
                default=(current_model is None),
            )
        ]
        # Discord allows up to 25 options per Select menu.
        for model_id, display_label in models[:24]:
            options.append(
                discord.SelectOption(
                    label=display_label[:100],
                    value=model_id[:100],
                    description=model_id[:100] if display_label != model_id else None,
                    default=(current_model == model_id),
                )
            )
        super().__init__(
            placeholder="Pilih model Antigravity...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.bot = bot
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        new_model = None if selected == "__default__" else selected
        self.bot.state_store.update(self.channel_id, model=new_model)
        label_map = {opt.value: opt.label for opt in self.options}
        chosen_label = label_map.get(selected, selected)
        for opt in self.options:
            opt.default = opt.value == selected
        await interaction.response.edit_message(
            content=f"✅ Model channel diset ke **{chosen_label}** (`{new_model or 'default'}`).",
            view=self.view,
        )


class ModelSelectView(discord.ui.View):
    """View wrapper enforcing user authorization for ModelSelect."""

    def __init__(
        self,
        bot: "AntigravityBot",
        channel_id: int,
        models: list[tuple[str, str]],
        current_model: str | None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.add_item(ModelSelect(bot, channel_id, models, current_model))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.bot.config.allowed_user_ids:
            await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
            return False
        return True


class AntigravityBot(discord.Client):
    """Discord gateway bot for Google Antigravity."""

    def __init__(self, config: GatewayConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)

        self.config = config
        self.state_store = StateStore(config.state_file)
        self.runner = AgyRunner(config.agy_bin)
        self._pending_memory_reload: set[int] = set()
        self.tree = app_commands.CommandTree(self)
        self._register_slash_commands()

    def _is_authorized_context(
        self, user_id: int, channel: discord.abc.Messageable | None
    ) -> bool:
        if channel is None or not hasattr(channel, "id"):
            return False
        channel_id = int(getattr(channel, "id"))
        parent_id_raw = getattr(channel, "parent_id", None)
        parent_id = int(parent_id_raw) if parent_id_raw is not None else None
        return self.config.is_authorized(
            user_id,
            channel_id,
            _is_dm_channel(channel),
            parent_channel_id=parent_id,
        )

    def _format_author_label(self, author: object) -> str:
        author_id = getattr(author, "id", None)
        if self.user is not None and author_id == self.user.id:
            return "Antigravity"
        name = str(getattr(author, "display_name", None) or getattr(author, "name", author_id))
        if getattr(author, "bot", False):
            return f"{name} (bot)"
        return name

    def _is_self_mentioned(self, message: discord.Message) -> bool:
        if self.user is None:
            return False
        if any(getattr(u, "id", None) == self.user.id for u in (message.mentions or ())):
            return True
        raw = message.content or ""
        return f"<@{self.user.id}>" in raw or f"<@!{self.user.id}>" in raw

    def _has_other_bot_mention(self, message: discord.Message) -> bool:
        self_id = self.user.id if self.user is not None else None
        return any(
            getattr(u, "bot", False) and getattr(u, "id", None) != self_id
            for u in (message.mentions or ())
        )

    def _is_other_bot_thread(self, channel: discord.abc.Messageable | None) -> bool:
        if not isinstance(channel, discord.Thread):
            return False
        owner_id = getattr(channel, "owner_id", None)
        if owner_id is None:
            return False
        if self.user is not None and owner_id == self.user.id:
            return False
        owner = getattr(channel, "owner", None)
        if owner is not None:
            return bool(getattr(owner, "bot", False))
        guild = getattr(channel, "guild", None)
        if guild is not None:
            member = guild.get_member(owner_id)
            if member is not None:
                return bool(getattr(member, "bot", False))
        return owner_id not in self.config.allowed_user_ids

    def _channel_trigger_mode(self, channel: discord.abc.Messageable | None) -> str:
        if channel is None:
            return "unknown"
        if _is_dm_channel(channel):
            return "DM (direct)"
        guild = getattr(channel, "guild", None)
        guild_id = getattr(guild, "id", None)
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        in_other_bot_thread = self._is_other_bot_thread(channel)
        is_free = self.config.is_free_response_channel(
            is_dm=False,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_id,
            in_other_bot_thread=in_other_bot_thread,
        )
        return "free-response" if is_free else "mention-only (`@mention`)"

    async def setup_hook(self) -> None:
        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        channels_desc = sorted(self.config.channel_ids) if self.config.channel_ids else "ALL"
        guilds_desc = [f"{g.name}({g.id})" for g in self.guilds]
        free_guilds = sorted(self.config.free_response_guild_ids) or "NONE"
        print(
            f"[agy-gateway] Logged in as {self.user} | guilds={guilds_desc} | "
            f"channels={channels_desc} | allow_dm={self.config.allow_dm} | "
            f"require_mention={self.config.require_mention} | free_guilds={free_guilds} | "
            f"backfill_limit={self.config.mention_backfill_limit}"
        )

    # ------------------------------------------------------------------
    # Shared command implementations (used by both /slash and !prefix)
    # ------------------------------------------------------------------

    async def _cmd_new(self, channel_id: int) -> str:
        if self.runner.is_running(channel_id):
            await self.runner.stop(channel_id)
        self.state_store.update(
            channel_id,
            conversation_id=None,
            last_context_tokens=None,
            last_input_tokens=None,
            last_cache_read_tokens=None,
            last_output_tokens=None,
        )
        return "✨ Sesi baru dimulai. `conversation_id` di-reset."

    async def _cmd_stop(self, channel_id: int) -> str:
        stopped = await self.runner.stop(channel_id)
        return "🛑 Task dihentikan." if stopped else "ℹ️ Tidak ada task yang sedang berjalan."

    async def _cmd_compact(self, channel_id: int, focus: str | None = None) -> str:
        ch = self.state_store.get(channel_id)
        old_conv_id = ch.conversation_id
        if not old_conv_id:
            return "ℹ️ Belum ada sesi aktif di channel ini untuk di-compact."
        if self.runner.is_running(channel_id):
            return "⏳ Tunggu task aktif selesai dulu (atau `/stop`) sebelum `/compact`."

        workdir = Path(ch.workdir) if ch.workdir else self.config.default_workdir
        mode = ch.mode or self.config.default_mode
        model = self.config.auxiliary_model or ch.model or self.config.default_model
        old_ctx = ch.last_context_tokens
        focus_note = f"\nFokus utama yang harus dipertahankan: {focus.strip()}" if focus and focus.strip() else ""
        summarize_prompt = (
            "Buat ringkasan handoff yang padat dan akurat (maksimal 400 kata) untuk memulai sesi baru "
            "tanpa menjalankan tool/edit apa pun. Sertakan:\n"
            "1. Tujuan utama & konteks kerja\n"
            "2. Keputusan penting & fakta teknis yang sudah terverifikasi\n"
            "3. File yang diubah / status test terakhir\n"
            "4. Langkah berikutnya yang belum selesai"
            f"{focus_note}"
        )

        summary_res = await self.runner.run_prompt(
            channel_id,
            summarize_prompt,
            workdir=workdir,
            conversation_id=old_conv_id,
            mode=mode,
            model=model,
            sandbox=self.config.sandbox,
            skip_permissions=self.config.skip_permissions,
            timeout_seconds=self.config.timeout_seconds,
            extra_dirs=hermes_extra_dirs(self.config),
        )
        if summary_res.error or not summary_res.response:
            err = summary_res.error or "ringkasan kosong"
            return f"❌ Gagal melakukan compact sesi `{old_conv_id}`: `{err}`"

        self.state_store.update(
            channel_id,
            conversation_id=None,
            last_context_tokens=None,
            last_input_tokens=None,
            last_cache_read_tokens=None,
            last_output_tokens=None,
        )
        self._pending_memory_reload.discard(channel_id)

        handoff_parts: list[str] = []
        hermes_block = build_hermes_context_block(self.config)
        if hermes_block:
            handoff_parts.append(hermes_block)
        handoff_parts.append(
            f"[Compacted Session Handoff from {old_conv_id}]\n"
            f"{summary_res.response}\n"
            f"[End Compacted Session Handoff]\n\n"
            "Konfirmasi dalam 1 kalimat singkat bahwa konteks handoff sudah dimuat dan siap lanjut."
        )
        fresh_res = await self.runner.run_prompt(
            channel_id,
            "\n\n".join(handoff_parts),
            workdir=workdir,
            conversation_id=None,
            mode=mode,
            model=model,
            sandbox=self.config.sandbox,
            skip_permissions=self.config.skip_permissions,
            timeout_seconds=self.config.timeout_seconds,
            extra_dirs=hermes_extra_dirs(self.config),
        )
        update_kwargs: dict[str, object] = {}
        if fresh_res.conversation_id:
            update_kwargs["conversation_id"] = fresh_res.conversation_id
        if fresh_res.context_tokens is not None:
            update_kwargs["last_context_tokens"] = fresh_res.context_tokens
        if fresh_res.usage:
            update_kwargs["last_input_tokens"] = fresh_res.usage.get("input_tokens")
            update_kwargs["last_cache_read_tokens"] = fresh_res.usage.get("cache_read_tokens")
            update_kwargs["last_output_tokens"] = fresh_res.usage.get("output_tokens")
        if update_kwargs:
            self.state_store.update(channel_id, **update_kwargs)  # type: ignore[arg-type]

        new_cid = fresh_res.conversation_id or "(pending)"
        new_ctx = fresh_res.context_tokens
        ctx_msg = ""
        if old_ctx is not None and new_ctx is not None:
            ctx_msg = f"\n- Context: `{old_ctx:,}` → `{new_ctx:,}` tokens"
        elif new_ctx is not None:
            ctx_msg = f"\n- Context baru: `{new_ctx:,}` tokens"
        return (
            f"🗜️ **Sesi di-compact!**\n"
            f"- Lama: `{old_conv_id}` → Baru: `{new_cid}`{ctx_msg}\n"
            f"- Ringkasan handoff udah disuntik ke sesi baru (ketik `/resume {old_conv_id}` kapan aja kalau mau balik ke sesi penuh)."
        )

    async def _cmd_status(
        self, channel_id: int, channel: discord.abc.Messageable | None = None
    ) -> str:
        ch = self.state_store.get(channel_id)
        running = "running ⏳" if self.runner.is_running(channel_id) else "idle ✅"
        conv = f"`{ch.conversation_id}`" if ch.conversation_id else "*(new session)*"
        model = f"`{ch.model or self.config.default_model or 'default'}`"
        mode = f"`{ch.mode or self.config.default_mode}`"
        workdir = f"`{ch.workdir or self.config.default_workdir}`"
        trigger = self._channel_trigger_mode(channel)
        hermes_files = [
            name
            for name, p in (
                ("SOUL", self.config.hermes_soul_file),
                ("USER", self.config.hermes_user_file),
                ("MEMORY", self.config.hermes_memory_file),
            )
            if p is not None and p.is_file()
        ]
        hermes_desc = (
            f"`{self.config.hermes_memory_mode}` ({', '.join(hermes_files)})"
            if self.config.hermes_memory_mode != "off" and hermes_files
            else "`off`"
        )
        effective_model = ch.model or self.config.default_model
        context_line = format_context_usage(
            ch.last_context_tokens,
            ch.last_input_tokens,
            ch.last_cache_read_tokens,
            ch.last_output_tokens,
            model_id=effective_model,
        )
        return (
            f"**Antigravity Gateway Status**\n"
            f"- State: {running}\n"
            f"- Trigger: {trigger} (backfill=`{self.config.mention_backfill_limit}`)\n"
            f"- Conversation: {conv}\n"
            f"- Context: {context_line}\n"
            f"- Mode: {mode} (sandbox=`{self.config.sandbox}`, skip_perm=`{self.config.skip_permissions}`)\n"
            f"- Model: {model}\n"
            f"- Workdir: {workdir}\n"
            f"- Hermes Context: {hermes_desc}"
        )

    async def _cmd_reload(self, channel_id: int) -> str:
        sync_res = sync_hermes_skills()
        sync_note = ""
        if sync_res.added or sync_res.removed:
            parts: list[str] = []
            if sync_res.added:
                parts.append(f"+{len(sync_res.added)} skill (`{', '.join(sync_res.added)}`)")
            if sync_res.removed:
                parts.append(f"-{len(sync_res.removed)} stale")
            sync_note = f"\n- Skill sync: {', '.join(parts)} (total `{sync_res.total_linked}` linked)"
        elif sync_res.total_linked:
            sync_note = f"\n- Skill sync: `{sync_res.total_linked}` skill Hermes aktif"

        block = build_hermes_context_block(self.config)
        if block is None:
            return f"ℹ️ Tidak ada file Hermes (`SOUL.md` / `USER.md` / `MEMORY.md`) yang aktif.{sync_note}"
        self._pending_memory_reload.add(channel_id)
        return (
            f"🔄 Context `SOUL.md` / `USER.md` / `MEMORY.md` / `AGY_MEMORY.md` "
            f"akan disuntikkan ulang di pesan berikutnya.{sync_note}"
        )

    async def _cmd_curator(self, channel_id: int, mode: str | None = None) -> str:
        ch = self.state_store.get(channel_id)
        workdir = Path(ch.workdir) if ch.workdir else self.config.default_workdir
        report = run_curator_check(workspace_skills_dir=workdir / ".agents" / "skills")
        summary = format_curator_report(report)

        clean_mode = (mode or "check").strip().lower()
        if clean_mode != "audit":
            return summary

        if self.runner.is_running(channel_id):
            return f"{summary}\n\n⏳ Channel sedang sibuk — lewati audit LLM."

        audit_prompt = (
            "[Agy Skill Curator Audit]\n"
            "Lakukan audit singkat skill & memory Agy berdasarkan laporan curator berikut:\n\n"
            f"{summary}\n\n"
            "Instruksi:\n"
            "1. Panggil MCP `memory_read` dan `session_search` untuk mengecek topik/perbaikan berulang terbaru.\n"
            "2. Cek apakah ada skill milik Agy (`agy-owned` / `workspace`) yang perlu diperbarui atau skill baru yang layak dibuat di `~/.gemini/config/skills/`.\n"
            "3. JANGAN ubah skill symlink milik `/root/.hermes/skills/`.\n"
            "4. Berikan rekomendasi ringkas dan spesifik dalam maksimal 8 bullet tanpa mengedit file dulu kecuali ada skill rusak."
        )
        res = await self.runner.run_prompt(
            channel_id,
            audit_prompt,
            workdir=workdir,
            conversation_id=None,
            mode="plan",
            model=self.config.auxiliary_model or ch.model or self.config.default_model,
            sandbox=self.config.sandbox,
            skip_permissions=self.config.skip_permissions,
            timeout_seconds=self.config.timeout_seconds,
            extra_dirs=hermes_extra_dirs(self.config),
        )
        if res.error:
            return f"{summary}\n\n❌ Audit LLM gagal: `{res.error}`"
        return f"{summary}\n\n**Rekomendasi Audit:**\n{res.response}"

    async def _cmd_search(self, query: str | None, source: str = "all") -> str:
        if not query or not query.strip():
            return "❌ Masukkan kata kunci pencarian. Contoh: `/search discord bot`"
        q = query.strip()

        def _run_search() -> list[SessionHit]:
            hits: list[SessionHit] = []
            if source in ("all", "antigravity"):
                hits.extend(
                    search_antigravity_sessions(self.config.agy_conversations_db, q, limit=5)
                )
            if source in ("all", "hermes") and self.config.hermes_state_db is not None:
                hits.extend(search_hermes_sessions(self.config.hermes_state_db, q, limit=5))
            return hits[:8]

        results = await asyncio.to_thread(_run_search)
        return format_search_results(q, results)

    async def _cmd_usage(self) -> str:
        try:
            payload = await self.runner.fetch_usage()
        except Exception as exc:
            return f"❌ Gagal mengambil data usage/limit: `{exc}`"
        return format_usage_report(payload)

    async def _build_model_picker(
        self, channel_id: int
    ) -> tuple[str, ModelSelectView | None]:
        models = await self.runner.fetch_models()
        current = self.state_store.get(channel_id).model or self.config.default_model
        current_label = current or "default"
        if not models:
            return f"Model aktif: `{current_label}`\n*(gagal mengambil daftar model dari `agy models`)*", None
        view = ModelSelectView(self, channel_id, models, current)
        return f"🤖 Model aktif: `{current_label}`\nPilih model dari dropdown di bawah:", view

    async def _cmd_model(self, channel_id: int, value: str | None) -> str:
        if not value:
            models_list = await self.runner.list_models()
            current = self.state_store.get(channel_id).model or self.config.default_model or "default"
            return f"Model aktif: `{current}`\n```\n{models_list[:1600]}\n```"
        cleaned = None if value.strip().lower() in ("default", "reset", "none") else value.strip()
        self.state_store.update(channel_id, model=cleaned)
        return f"✅ Model channel diset ke `{cleaned or 'default'}`."

    async def _cmd_mode(self, channel_id: int, value: str | None) -> str:
        if not value:
            current = self.state_store.get(channel_id).mode or self.config.default_mode
            return f"Mode aktif: `{current}` (pilihan: {', '.join(VALID_MODES)})"
        cleaned = value.strip()
        if cleaned not in VALID_MODES:
            return f"❌ Mode tidak valid. Pilih salah satu: {', '.join(VALID_MODES)}"
        self.state_store.update(channel_id, mode=cleaned)
        return f"✅ Execution mode diset ke `{cleaned}`."

    async def _cmd_workdir(self, channel_id: int, value: str | None) -> str:
        if not value:
            current = self.state_store.get(channel_id).workdir or str(self.config.default_workdir)
            return f"Workdir aktif: `{current}`"
        target = Path(value.strip()).expanduser().resolve()
        if not target.is_dir():
            return f"❌ Direktori tidak ditemukan: `{target}`"
        self.state_store.update(channel_id, workdir=str(target))
        return f"✅ Workdir channel diset ke `{target}`."

    async def _cmd_resume(self, channel_id: int, conv_id: str | None) -> str:
        if not conv_id or not conv_id.strip():
            return "❌ Masukkan conversation ID. Contoh: `!resume <conversation_id>`"
        self.state_store.update(
            channel_id,
            conversation_id=conv_id.strip(),
            last_context_tokens=None,
            last_input_tokens=None,
            last_cache_read_tokens=None,
            last_output_tokens=None,
        )
        return f"🔗 Channel ini sekarang tersambung ke conversation `{conv_id.strip()}`."

    def _cmd_help(self) -> str:
        return (
            "**Antigravity Discord Gateway Commands**\n"
            "Kirim pesan biasa di channel ini untuk langsung ngobrol dengan Antigravity.\n\n"
            "**Commands (`/` atau `!`):**\n"
            "- `/new` / `!new` — Mulai sesi conversation baru\n"
            "- `/compact [fokus]` / `!compact [fokus]` — Ringkas sesi & lanjut di context fresh\n"
            "- `/stop` / `!stop` — Hentikan task yang sedang jalan\n"
            "- `/status` / `!status` — Cek conversation ID, context window, mode, model, & workdir\n"
            "- `/model [nama]` / `!model [nama]` — Lihat list atau ganti model\n"
            "- `/mode [auto|accept-edits|plan]` / `!mode ...` — Ganti mode eksekusi\n"
            "- `/workdir [path]` / `!workdir [path]` — Ganti folder workspace\n"
            "- `/resume <id>` / `!resume <id>` — Lanjutkan conversation ID tertentu\n"
            "- `/reload` / `!reload` — Muat ulang `SOUL.md` & `MEMORY.md` Hermes + sync skill\n"
            "- `/curator [audit]` / `!curator [audit]` — Cek integritas skill & ledger curator (tambah `audit` untuk review LLM)\n"
            "- `/search <kata>` / `!search <kata>` — Cari histori sesi Antigravity & Hermes\n"
            "- `/usage` / `!usage` (`/quota`) — Cek sisa limit & waktu reset model\n"
            "- Mention `#nama-channel` di pesan untuk menarik konteks percakapan channel lain\n"
            "- Di server *mention-only*, tag `@Antigravity` (bisa digabung `!cmd` atau reply pesan)"
        )

    def _register_slash_commands(self) -> None:
        async def _guarded(
            interaction: discord.Interaction,
            handler: Callable[[int], Awaitable[str]],
        ) -> None:
            if not self._is_authorized_context(interaction.user.id, interaction.channel):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return
            await interaction.response.defer(thinking=False)
            assert interaction.channel_id is not None
            reply = await handler(interaction.channel_id)
            for chunk in split_message(reply):
                await interaction.followup.send(chunk)

        @self.tree.command(name="new", description="Mulai sesi Antigravity baru di channel ini")
        async def slash_new(interaction: discord.Interaction) -> None:
            await _guarded(interaction, self._cmd_new)

        @self.tree.command(
            name="compact",
            description="Ringkas sesi aktif lalu lanjutkan di sesi baru yang ringan",
        )
        @app_commands.describe(focus="Fokus topik/detail yang wajib dipertahankan (opsional)")
        async def slash_compact(
            interaction: discord.Interaction, focus: str | None = None
        ) -> None:
            await _guarded(interaction, lambda cid: self._cmd_compact(cid, focus))

        @self.tree.command(name="stop", description="Hentikan task Antigravity yang sedang berjalan")
        async def slash_stop(interaction: discord.Interaction) -> None:
            await _guarded(interaction, self._cmd_stop)

        @self.tree.command(name="status", description="Lihat status sesi Antigravity channel ini")
        async def slash_status(interaction: discord.Interaction) -> None:
            await _guarded(interaction, lambda cid: self._cmd_status(cid, interaction.channel))

        @self.tree.command(name="model", description="Lihat atau ganti model Antigravity")
        @app_commands.describe(name="Nama model (kosongkan untuk membuka dropdown pilihan)")
        async def slash_model(interaction: discord.Interaction, name: str | None = None) -> None:
            if not self._is_authorized_context(interaction.user.id, interaction.channel):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return
            await interaction.response.defer(thinking=False)
            assert interaction.channel_id is not None
            if name:
                reply = await self._cmd_model(interaction.channel_id, name)
                await interaction.followup.send(reply)
                return
            text, view = await self._build_model_picker(interaction.channel_id)
            if view is not None:
                await interaction.followup.send(text, view=view)
            else:
                await interaction.followup.send(text)

        @slash_model.autocomplete("name")
        async def slash_model_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id not in self.config.allowed_user_ids:
                return []
            models = await self.runner.fetch_models()
            needle = current.strip().lower()
            choices: list[app_commands.Choice[str]] = []
            if not needle or "default".startswith(needle):
                choices.append(app_commands.Choice(name="Default (reset ke config)", value="default"))
            for model_id, label in models:
                if not needle or needle in model_id.lower() or needle in label.lower():
                    choices.append(app_commands.Choice(name=f"{label} ({model_id})"[:100], value=model_id))
                if len(choices) >= 25:
                    break
            return choices

        @self.tree.command(name="mode", description="Lihat atau ganti execution mode")
        @app_commands.choices(
            mode=[app_commands.Choice(name=m, value=m) for m in VALID_MODES]
        )
        async def slash_mode(
            interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None
        ) -> None:
            val = mode.value if mode else None
            await _guarded(interaction, lambda cid: self._cmd_mode(cid, val))

        @self.tree.command(name="workdir", description="Lihat atau ganti folder workspace")
        @app_commands.describe(path="Path direktori absolut di host")
        async def slash_workdir(
            interaction: discord.Interaction, path: str | None = None
        ) -> None:
            await _guarded(interaction, lambda cid: self._cmd_workdir(cid, path))

        @self.tree.command(name="resume", description="Sambungkan channel ke conversation ID tertentu")
        @app_commands.describe(conversation_id="ID conversation Antigravity")
        async def slash_resume(interaction: discord.Interaction, conversation_id: str) -> None:
            await _guarded(interaction, lambda cid: self._cmd_resume(cid, conversation_id))

        @self.tree.command(name="reload", description="Muat ulang Hermes SOUL.md / MEMORY.md / USER.md")
        async def slash_reload(interaction: discord.Interaction) -> None:
            await _guarded(interaction, self._cmd_reload)

        @self.tree.command(
            name="curator",
            description="Jalankan Agy Skill Curator (sync, validasi SKILL.md, atau audit LLM)",
        )
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="check (cepat, 0 token)", value="check"),
                app_commands.Choice(name="audit (review mendalam dengan LLM)", value="audit"),
            ]
        )
        async def slash_curator(
            interaction: discord.Interaction,
            mode: app_commands.Choice[str] | None = None,
        ) -> None:
            val = mode.value if mode else "check"
            await _guarded(interaction, lambda cid: self._cmd_curator(cid, val))

        @self.tree.command(name="search", description="Cari histori percakapan Antigravity & Hermes")
        @app_commands.describe(query="Kata kunci yang ingin dicari")
        @app_commands.choices(
            source=[
                app_commands.Choice(name="all", value="all"),
                app_commands.Choice(name="antigravity", value="antigravity"),
                app_commands.Choice(name="hermes", value="hermes"),
            ]
        )
        async def slash_search(
            interaction: discord.Interaction,
            query: str,
            source: app_commands.Choice[str] | None = None,
        ) -> None:
            src = source.value if source else "all"
            await _guarded(interaction, lambda _cid: self._cmd_search(query, src))

        @self.tree.command(name="usage", description="Cek sisa limit & kuota model Antigravity")
        async def slash_usage(interaction: discord.Interaction) -> None:
            await _guarded(interaction, lambda _cid: self._cmd_usage())

        @self.tree.command(name="quota", description="Alias /usage: cek sisa limit model Antigravity")
        async def slash_quota(interaction: discord.Interaction) -> None:
            await _guarded(interaction, lambda _cid: self._cmd_usage())

        @self.tree.command(name="help", description="Tampilkan bantuan Antigravity Gateway")
        async def slash_help(interaction: discord.Interaction) -> None:
            await _guarded(interaction, lambda _cid: asyncio.sleep(0, result=self._cmd_help()))

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self._is_authorized_context(message.author.id, message.channel):
            return

        is_dm = _is_dm_channel(message.channel)
        guild = getattr(message.channel, "guild", None) or getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None)
        channel_id = message.channel.id
        parent_id = getattr(message.channel, "parent_id", None)
        self_mentioned = self._is_self_mentioned(message)
        other_bot_mentioned = self._has_other_bot_mention(message)
        in_other_bot_thread = self._is_other_bot_thread(message.channel)

        if not self.config.should_respond_to_message(
            is_dm=is_dm,
            self_mentioned=self_mentioned,
            other_bot_mentioned=other_bot_mentioned,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_id,
            in_other_bot_thread=in_other_bot_thread,
        ):
            return

        content = (message.content or "").strip()
        if self_mentioned and self.user is not None:
            content = strip_bot_mention(content, self.user.id)

        if content.startswith("!"):
            handled = await self._handle_prefix_command(message, content)
            if handled:
                return

        if not content and not message.attachments:
            if message.reference is not None:
                content = (
                    "Tolong tanggapi atau review pesan yang di-reply "
                    "dan konteks percakapan terbaru di channel ini."
                )
            else:
                return

        if self.runner.is_running(channel_id):
            await message.reply(
                "⏳ Masih ada task yang jalan di channel ini. "
                "Tunggu selesai atau ketik `!stop` / `/stop` untuk membatalkan.",
                mention_author=False,
            )
            return

        ch_state = self.state_store.get(channel_id)
        workdir = Path(ch_state.workdir) if ch_state.workdir else self.config.default_workdir
        prompt = await self._build_prompt_with_attachments(message, content, workdir)
        context_parts: list[str] = []
        backfill_context = await self._resolve_channel_backfill_context(message)
        if backfill_context:
            context_parts.append(backfill_context)
        cross_context = await self._resolve_cross_channel_context(message, content)
        if cross_context:
            context_parts.append(cross_context)
        if context_parts:
            joined_ctx = "\n\n".join(context_parts)
            prompt = f"{joined_ctx}\n\n[Current User Request]\n{prompt}"
        await self._execute_and_stream(message, prompt, workdir)

    async def _handle_prefix_command(self, message: discord.Message, content: str) -> bool:
        parts = content[1:].split(maxsplit=1)
        if not parts:
            return False
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else None
        cid = message.channel.id

        dispatch: dict[str, Callable[[], Awaitable[str]]] = {
            "new": lambda: self._cmd_new(cid),
            "compact": lambda: self._cmd_compact(cid, arg),
            "stop": lambda: self._cmd_stop(cid),
            "status": lambda: self._cmd_status(cid, message.channel),
            "model": lambda: self._cmd_model(cid, arg),
            "mode": lambda: self._cmd_mode(cid, arg),
            "workdir": lambda: self._cmd_workdir(cid, arg),
            "resume": lambda: self._cmd_resume(cid, arg),
            "reload": lambda: self._cmd_reload(cid),
            "curator": lambda: self._cmd_curator(cid, arg),
            "search": lambda: self._cmd_search(arg),
            "usage": lambda: self._cmd_usage(),
            "quota": lambda: self._cmd_usage(),
            "help": lambda: asyncio.sleep(0, result=self._cmd_help()),
        }
        if cmd == "model" and not arg:
            text, view = await self._build_model_picker(cid)
            if view is not None:
                await message.reply(text, view=view, mention_author=False)
            else:
                await message.reply(text, mention_author=False)
            return True
        handler = dispatch.get(cmd)
        if handler is None:
            return False
        reply = await handler()
        for chunk in split_message(reply):
            await message.reply(chunk, mention_author=False)
        return True

    async def _build_prompt_with_attachments(
        self, message: discord.Message, content: str, workdir: Path
    ) -> str:
        if not message.attachments:
            return content
        upload_dir = workdir / ".gateway_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        for att in message.attachments:
            dest = upload_dir / f"{message.id}_{_sanitize_filename(att.filename)}"
            await att.save(dest)
            saved_paths.append(str(dest))
        files_note = "\n".join(f"- {p}" for p in saved_paths)
        base = content or "Tolong cek file lampiran ini."
        return f"{base}\n\n[Attached files downloaded from Discord]\n{files_note}"

    async def _resolve_channel_backfill_context(
        self, message: discord.Message
    ) -> str | None:
        """Pull missed recent messages since Antigravity's last reply in this guild channel."""
        if _is_dm_channel(message.channel):
            return None

        replied_message: tuple[str, str] | None = None
        ref = getattr(message, "reference", None)
        if ref is not None:
            ref_msg = getattr(ref, "resolved", None)
            if (
                (ref_msg is None or not hasattr(ref_msg, "content"))
                and getattr(ref, "message_id", None) is not None
                and hasattr(message.channel, "fetch_message")
            ):
                try:
                    ref_msg = await message.channel.fetch_message(ref.message_id)  # type: ignore[attr-defined]
                except discord.DiscordException:
                    ref_msg = None
            if ref_msg is not None and hasattr(ref_msg, "content"):
                ref_text = (getattr(ref_msg, "content", "") or "").strip()
                if ref_text and ref_text != "⏳ Memulai Antigravity...":
                    replied_message = (
                        self._format_author_label(ref_msg.author),
                        ref_text,
                    )

        missed: list[tuple[str, str]] = []
        limit = self.config.mention_backfill_limit
        if limit > 0 and hasattr(message.channel, "history"):
            try:
                async for msg in message.channel.history(limit=limit, before=message):  # type: ignore[attr-defined]
                    if self.user is not None and getattr(msg.author, "id", None) == self.user.id:
                        break
                    text = (msg.content or "").strip()
                    if not text or text == "⏳ Memulai Antigravity...":
                        continue
                    missed.append((self._format_author_label(msg.author), text))
            except discord.DiscordException:
                pass
            missed.reverse()

        ch_name = str(getattr(message.channel, "name", message.channel.id))
        return format_channel_backfill_block(
            ch_name,
            missed,
            replied_message=replied_message,
        )

    async def _resolve_cross_channel_context(
        self, message: discord.Message, content: str
    ) -> str | None:
        mentioned_ids = extract_channel_mentions(content, exclude_channel_id=message.channel.id)
        if not mentioned_ids:
            return None

        blocks: list[str] = []
        for ref_id in mentioned_ids:
            ref_ch = self.get_channel(ref_id)
            if ref_ch is None:
                try:
                    ref_ch = await self.fetch_channel(ref_id)
                except discord.DiscordException:
                    continue
            if not hasattr(ref_ch, "history"):
                continue
            ref_guild = getattr(ref_ch, "guild", None)
            if message.guild is not None and ref_guild != message.guild:
                continue

            recent: list[tuple[str, str]] = []
            try:
                async for msg in ref_ch.history(limit=self.config.cross_channel_history_limit):  # type: ignore[attr-defined]
                    text = (msg.content or "").strip()
                    if not text or text == "⏳ Memulai Antigravity...":
                        continue
                    recent.append((self._format_author_label(msg.author), text))
            except discord.DiscordException:
                continue

            recent.reverse()
            ch_name = getattr(ref_ch, "name", str(ref_id))
            conv_id = self.state_store.get(ref_id).conversation_id
            blocks.append(
                format_referenced_channel_block(
                    ch_name, ref_id, recent, conversation_id=conv_id
                )
            )

        if not blocks:
            return None
        joined = "\n\n".join(blocks)
        return f"[Referenced Discord Channel Context]\n{joined}\n[End Referenced Context]"

    async def _execute_and_stream(
        self, message: discord.Message, prompt: str, workdir: Path
    ) -> None:
        channel = message.channel
        channel_id = channel.id
        ch_state = self.state_store.get(channel_id)
        needs_hermes = ch_state.conversation_id is None or channel_id in self._pending_memory_reload
        effective_prompt = prompt
        if needs_hermes:
            sync_hermes_skills()
            hermes_block = build_hermes_context_block(self.config)
            if hermes_block:
                effective_prompt = f"{hermes_block}\n\n[User Message]\n{prompt}"

        placeholder = await message.reply("⏳ Memulai Antigravity...", mention_author=False)
        last_edit_ts = time.monotonic()
        last_sent_text = ""

        async def on_progress(accumulated: str, status_line: str) -> None:
            nonlocal last_edit_ts, last_sent_text
            now = time.monotonic()
            if now - last_edit_ts < self.config.stream_edit_interval:
                return
            preview = format_streaming_preview(accumulated, status_line)
            if preview == last_sent_text:
                return
            last_edit_ts = now
            last_sent_text = preview
            try:
                await placeholder.edit(content=preview)
            except discord.HTTPException:
                pass

        async with channel.typing():
            result = await self.runner.run_prompt(
                channel_id,
                effective_prompt,
                workdir=workdir,
                conversation_id=ch_state.conversation_id,
                mode=ch_state.mode or self.config.default_mode,
                model=ch_state.model or self.config.default_model,
                sandbox=self.config.sandbox,
                skip_permissions=self.config.skip_permissions,
                timeout_seconds=self.config.timeout_seconds,
                extra_dirs=hermes_extra_dirs(self.config),
                on_progress=on_progress,
            )

        self._pending_memory_reload.discard(channel_id)
        update_kwargs: dict[str, object] = {}
        if result.conversation_id:
            update_kwargs["conversation_id"] = result.conversation_id
        if result.context_tokens is not None:
            update_kwargs["last_context_tokens"] = result.context_tokens
        if result.usage:
            update_kwargs["last_input_tokens"] = result.usage.get("input_tokens")
            update_kwargs["last_cache_read_tokens"] = result.usage.get("cache_read_tokens")
            update_kwargs["last_output_tokens"] = result.usage.get("output_tokens")
        if update_kwargs:
            self.state_store.update(channel_id, **update_kwargs)  # type: ignore[arg-type]

        if result.cancelled:
            body = result.response or "*(task dihentikan oleh user)*"
            final_text = f"{body}\n\n🛑 *Dibatalkan.*"
        elif result.error:
            prefix = f"{result.response}\n\n" if result.response else ""
            final_text = f"{prefix}❌ **Error:** {result.error}"
        else:
            final_text = result.response or "*(selesai tanpa output teks)*"
            warn = format_context_warning(
                result.context_tokens,
                model_id=ch_state.model or self.config.default_model,
                threshold=self.config.context_warn_threshold,
            )
            if warn:
                final_text = f"{final_text}\n\n{warn}"

        chunks = split_message(final_text)
        try:
            await placeholder.edit(content=chunks[0])
        except discord.HTTPException:
            await channel.send(chunks[0])

        for extra in chunks[1:]:
            await channel.send(extra)
