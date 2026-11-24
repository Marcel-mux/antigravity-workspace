"""CLI entrypoint (`setup`, `start`, `service`) for the Antigravity Discord Gateway."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agy_gateway.bot import AntigravityBot
from agy_gateway.config import VALID_MEMORY_MODES, VALID_MODES, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt_text = f"{label}{suffix}: "
    raw = getpass.getpass(prompt_text) if secret else input(prompt_text)
    cleaned = raw.strip()
    return cleaned if cleaned else default


def _prompt_bool(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{label} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true")


def render_systemd_unit(project_dir: Path, python_bin: Path, user: str) -> str:
    """Return a systemd unit file string for running the Discord gateway."""
    return (
        "[Unit]\n"
        "Description=Antigravity Discord Gateway\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory={project_dir}\n"
        f"EnvironmentFile={project_dir / '.env'}\n"
        f"ExecStart={python_bin} {project_dir / 'gateway.py'} start\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def run_setup(env_path: Path) -> int:
    """Interactive Hermes-style setup wizard to configure .env and optional systemd unit."""
    print("=== Antigravity Discord Gateway Setup ===")
    print("Siapkan Bot Token & IDs dari Discord Developer Portal / Discord App.\n")

    token = _prompt("1. Discord Bot Token", secret=True)
    while not token:
        print("Bot Token wajib diisi.")
        token = _prompt("1. Discord Bot Token", secret=True)

    allowed_users = _prompt("2. Allowed Discord User ID(s) (pisahkan koma)")
    while not allowed_users:
        print("Minimal 1 Discord User ID wajib diisi demi keamanan.")
        allowed_users = _prompt("2. Allowed Discord User ID(s) (pisahkan koma)")

    channel_ids = _prompt(
        "3. Whitelist Channel ID(s) (opsional, kosongkan = aktif di SEMUA channel server)"
    )
    allow_dm = _prompt_bool("4. Izinkan chat via DM ke bot?", default=True)
    guild_id = _prompt("5. Discord Server/Guild ID (opsional, untuk sync slash command instan)")

    workdir = _prompt("6. Default Workspace Directory", default=str(PROJECT_ROOT))
    mode = _prompt(f"7. Execution Mode ({'/'.join(VALID_MODES)})", default="auto")
    if mode not in VALID_MODES:
        print(f"Mode '{mode}' tidak dikenal, fallback ke 'auto'.")
        mode = "auto"

    model = _prompt("8. Default Model (kosongkan untuk pakai default agy)", default="")
    sandbox = _prompt_bool("9. Aktifkan --sandbox untuk perintah terminal?", default=False)
    skip_perm = _prompt_bool(
        "10. Aktifkan --dangerously-skip-permissions (auto-approve tool saat remote)?",
        default=False,
    )

    hermes_home = Path.home() / ".hermes"
    default_soul = hermes_home / "SOUL.md"
    default_mem = hermes_home / "memories" / "MEMORY.md"
    default_user = hermes_home / "memories" / "USER.md"
    has_hermes = any(p.is_file() for p in (default_soul, default_mem, default_user))

    soul_path = ""
    mem_path = ""
    user_path = ""
    mem_mode = "off"
    if _prompt_bool(
        "11. Pakai SOUL.md / MEMORY.md / USER.md dari Hermes?",
        default=has_hermes,
    ):
        soul_path = _prompt("    Path SOUL.md", default=str(default_soul) if default_soul.is_file() else "")
        mem_path = _prompt("    Path MEMORY.md", default=str(default_mem) if default_mem.is_file() else "")
        user_path = _prompt("    Path USER.md", default=str(default_user) if default_user.is_file() else "")
        mem_mode = _prompt(
            f"    Mode Hermes memory ({'/'.join(VALID_MEMORY_MODES)})",
            default="readonly",
        )
        if mem_mode not in VALID_MEMORY_MODES:
            print(f"Mode memory '{mem_mode}' tidak dikenal, fallback ke 'readonly'.")
            mem_mode = "readonly"

    agy_bin = shutil.which("agy") or "agy"
    env_content = (
        f"DISCORD_BOT_TOKEN={token}\n"
        f"DISCORD_ALLOWED_USER_IDS={allowed_users}\n"
        f"DISCORD_CHANNEL_IDS={channel_ids}\n"
        f"DISCORD_ALLOW_DM={'true' if allow_dm else 'false'}\n"
        f"DISCORD_GUILD_ID={guild_id}\n"
        "DISCORD_REQUIRE_MENTION=false\n"
        "DISCORD_FREE_RESPONSE_GUILD_IDS=\n"
        "DISCORD_FREE_RESPONSE_CHANNEL_IDS=\n"
        "DISCORD_IGNORE_OTHER_BOT_MENTIONS=true\n"
        "DISCORD_MENTION_BACKFILL_LIMIT=15\n"
        f"AGY_BIN={agy_bin}\n"
        f"AGY_WORKDIR={workdir}\n"
        f"AGY_MODE={mode}\n"
        f"AGY_MODEL={model}\n"
        f"AGY_SANDBOX={'true' if sandbox else 'false'}\n"
        f"AGY_SKIP_PERMISSIONS={'true' if skip_perm else 'false'}\n"
        "AGY_TIMEOUT_SECONDS=0\n"
        "GATEWAY_STATE_FILE=state.json\n"
        "STREAM_EDIT_INTERVAL_SECONDS=1.2\n"
        f"HERMES_MEMORY_MODE={mem_mode}\n"
        f"HERMES_SOUL_FILE={soul_path}\n"
        f"HERMES_MEMORY_FILE={mem_path}\n"
        f"HERMES_USER_FILE={user_path}\n"
    )

    env_path.write_text(env_content, encoding="utf-8")
    os.chmod(env_path, 0o600)
    print(f"\n✅ Konfigurasi disimpan ke {env_path} (mode 600).")

    if _prompt_bool("Generate file systemd service (agy-gateway.service)?", default=True):
        _write_or_install_service(install_prompt=True)

    print("\nJalankan bot sekarang dengan:")
    print(f"  {sys.executable} {PROJECT_ROOT / 'gateway.py'} start")
    return 0


def _write_or_install_service(*, install_prompt: bool = False) -> Path:
    user = os.getenv("USER", "root")
    unit_text = render_systemd_unit(PROJECT_ROOT, Path(sys.executable), user)
    unit_out = PROJECT_ROOT / "agy-gateway.service"
    unit_out.write_text(unit_text, encoding="utf-8")
    print(f"📄 File systemd unit dibuat di: {unit_out}")

    systemd_dir = Path("/etc/systemd/system")
    if install_prompt and os.geteuid() == 0 and systemd_dir.is_dir():
        if _prompt_bool("Install & enable agy-gateway.service ke systemd sekarang?", default=False):
            target = systemd_dir / "agy-gateway.service"
            target.write_text(unit_text, encoding="utf-8")
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "agy-gateway.service"], check=True)
            print("✅ Service terpasang! Start dengan: systemctl start agy-gateway")
    return unit_out


def run_start(env_path: Path) -> int:
    """Start the Discord gateway bot."""
    from agy_gateway.curator import run_curator_check

    config = load_config(env_path if env_path.exists() else None)
    try:
        run_curator_check(workspace_skills_dir=config.default_workdir / ".agents" / "skills")
    except OSError:
        pass
    bot = AntigravityBot(config)
    bot.run(config.discord_bot_token)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch subcommands."""
    parser = argparse.ArgumentParser(
        prog="gateway.py",
        description="Antigravity Discord Gateway (Hermes-style bridge for `agy` CLI)",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Path to .env config file (default: ./.env)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Interactive setup wizard (.env + systemd unit)")
    sub.add_parser("start", help="Start the Discord gateway bot")
    sub.add_parser("service", help="Generate agy-gateway.service systemd unit file")

    args = parser.parse_args(argv)
    cmd = args.command or "start"

    if cmd == "setup":
        return run_setup(args.env)
    if cmd == "service":
        _write_or_install_service(install_prompt=True)
        return 0
    return run_start(args.env)
