"""Skill integrity checker, state tracker, and audit ledger for Agy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agy_gateway.skill_sync import (
    DEFAULT_AGY_SKILLS_DIR,
    DEFAULT_HERMES_SKILLS_DIR,
    SkillSyncResult,
    _points_into_dir,
    sync_hermes_skills,
)

DEFAULT_CURATOR_STATE = Path.home() / ".gemini" / "config" / ".curator_state.json"
DEFAULT_CURATOR_LEDGER = Path.home() / ".gemini" / "config" / ".curator_ledger.jsonl"
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
DEFAULT_STALE_DAYS = 90


@dataclass
class SkillHealth:
    """Audit status of a single discovered skill directory."""

    name: str
    source: str  # "hermes-symlink" | "agy-owned" | "workspace"
    path: Path
    sha256: str | None = None
    issues: list[str] = field(default_factory=list)
    stale: bool = False
    age_days: int = 0


@dataclass
class CuratorReport:
    """Result of running an Agy skill curator check."""

    run_count: int
    last_run_at: str
    duration_seconds: float
    sync_result: SkillSyncResult
    skills: list[SkillHealth] = field(default_factory=list)
    ledger_events: int = 0
    state_file: Path = DEFAULT_CURATOR_STATE
    ledger_file: Path = DEFAULT_CURATOR_LEDGER

    @property
    def invalid_skills(self) -> list[SkillHealth]:
        return [s for s in self.skills if s.issues]

    @property
    def stale_skills(self) -> list[SkillHealth]:
        return [s for s in self.skills if s.stale and not s.issues]

    @property
    def summary_line(self) -> str:
        return (
            f"total={len(self.skills)}, invalid={len(self.invalid_skills)}, "
            f"stale={len(self.stale_skills)}, synced=+{len(self.sync_result.added)}/-{len(self.sync_result.removed)}"
        )


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in m.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line[0].isspace() and ":" in raw_line:
            key, _, val = raw_line.partition(":")
            current_key = key.strip()
            cleaned = val.strip().strip('"').strip("'")
            fields[current_key] = "" if cleaned in (">-", ">", "|-", "|") else cleaned
        elif current_key and raw_line[0].isspace():
            extra = raw_line.strip()
            if extra:
                prev = fields.get(current_key, "")
                fields[current_key] = f"{prev} {extra}".strip() if prev else extra
    return fields


def _inspect_skill_dir(
    skill_dir: Path,
    *,
    source: str,
    now_ts: float,
    stale_days: int,
) -> SkillHealth:
    issues: list[str] = []
    sha: str | None = None
    age_days = 0
    stale = False

    if skill_dir.is_symlink() and not skill_dir.exists():
        issues.append("broken symlink")
        return SkillHealth(name=skill_dir.name, source=source, path=skill_dir, issues=issues)

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        issues.append("missing SKILL.md")
        return SkillHealth(name=skill_dir.name, source=source, path=skill_dir, issues=issues)

    try:
        raw_bytes = skill_md.read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8", errors="replace")
        mtime = skill_md.stat().st_mtime
        age_days = max(0, int((now_ts - mtime) / 86400))
    except OSError as exc:
        issues.append(f"read error: {exc}")
        return SkillHealth(name=skill_dir.name, source=source, path=skill_dir, issues=issues)

    fm = _parse_frontmatter(text)
    if fm is None:
        issues.append("missing YAML frontmatter")
    else:
        if not fm.get("name"):
            issues.append("missing frontmatter 'name'")
        if not fm.get("description"):
            issues.append("missing frontmatter 'description'")

    if source != "hermes-symlink" and age_days >= stale_days:
        stale = True

    return SkillHealth(
        name=skill_dir.name,
        source=source,
        path=skill_dir,
        sha256=sha,
        issues=issues,
        stale=stale,
        age_days=age_days,
    )


def _load_previous_state(state_file: Path) -> dict[str, Any]:
    if not state_file.is_file():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _append_ledger(ledger_file: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    with ledger_file.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_curator_check(
    *,
    hermes_skills_dir: Path = DEFAULT_HERMES_SKILLS_DIR,
    agy_skills_dir: Path = DEFAULT_AGY_SKILLS_DIR,
    workspace_skills_dir: Path | None = None,
    state_file: Path = DEFAULT_CURATOR_STATE,
    ledger_file: Path = DEFAULT_CURATOR_LEDGER,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> CuratorReport:
    """Sync Hermes skills, audit all Agy/workspace skills, and update curator state + ledger."""
    t0 = time.monotonic()
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    sync_res = sync_hermes_skills(hermes_skills_dir, agy_skills_dir)

    skills: list[SkillHealth] = []
    scan_roots: list[tuple[Path, str]] = [(agy_skills_dir, "global")]
    if workspace_skills_dir is not None and workspace_skills_dir.is_dir():
        scan_roots.append((workspace_skills_dir, "workspace"))

    for root, scope in scan_roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                continue
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if scope == "workspace":
                source = "workspace"
            elif entry.is_symlink() and _points_into_dir(entry, hermes_skills_dir):
                source = "hermes-symlink"
            else:
                source = "agy-owned"
            skills.append(
                _inspect_skill_dir(entry, source=source, now_ts=now_ts, stale_days=stale_days)
            )

    prev_state = _load_previous_state(state_file)
    prev_hashes: dict[str, str] = (
        prev_state.get("hashes") if isinstance(prev_state.get("hashes"), dict) else {}
    )
    run_count = int(prev_state.get("run_count") or 0) + 1

    new_hashes: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for sk in skills:
        key = f"{sk.source}:{sk.name}"
        if sk.sha256:
            new_hashes[key] = sk.sha256
            old_sha = prev_hashes.get(key)
            if old_sha is None:
                events.append(
                    {"ts": now_iso, "action": "added", "skill": sk.name, "source": sk.source, "sha256": sk.sha256}
                )
            elif old_sha != sk.sha256:
                events.append(
                    {
                        "ts": now_iso,
                        "action": "updated",
                        "skill": sk.name,
                        "source": sk.source,
                        "before": old_sha,
                        "after": sk.sha256,
                    }
                )
        if sk.issues:
            events.append(
                {"ts": now_iso, "action": "invalid", "skill": sk.name, "source": sk.source, "issues": sk.issues}
            )

    for removed_name in sync_res.removed:
        events.append({"ts": now_iso, "action": "pruned_symlink", "skill": removed_name})

    _append_ledger(ledger_file, events)

    duration = round(time.monotonic() - t0, 4)
    report = CuratorReport(
        run_count=run_count,
        last_run_at=now_iso,
        duration_seconds=duration,
        sync_result=sync_res,
        skills=skills,
        ledger_events=len(events),
        state_file=state_file,
        ledger_file=ledger_file,
    )

    state_payload = {
        "last_run_at": now_iso,
        "last_run_duration_seconds": duration,
        "last_run_summary": report.summary_line,
        "run_count": run_count,
        "totals": {
            "total": len(skills),
            "hermes_linked": sum(1 for s in skills if s.source == "hermes-symlink"),
            "agy_owned": sum(1 for s in skills if s.source == "agy-owned"),
            "workspace": sum(1 for s in skills if s.source == "workspace"),
            "invalid": len(report.invalid_skills),
            "stale": len(report.stale_skills),
        },
        "hashes": new_hashes,
    }
    _atomic_write_json(state_file, state_payload)
    return report


def format_curator_report(report: CuratorReport) -> str:
    """Format a CuratorReport cleanly for Discord output."""
    hermes_cnt = sum(1 for s in report.skills if s.source == "hermes-symlink")
    owned_cnt = sum(1 for s in report.skills if s.source == "agy-owned")
    ws_cnt = sum(1 for s in report.skills if s.source == "workspace")
    lines = [
        f"🧹 **Agy Skill Curator (Run #{report.run_count})**",
        f"- Total skill aktif: `{len(report.skills)}` (`{hermes_cnt}` Hermes-linked, `{owned_cnt}` Agy-owned, `{ws_cnt}` workspace)",
        f"- Sync Hermes: `+{len(report.sync_result.added)}` added / `-{len(report.sync_result.removed)}` pruned",
        f"- Ledger events baru: `{report.ledger_events}` (`{report.duration_seconds * 1000:.1f} ms`)",
    ]
    if report.sync_result.added:
        lines.append(f"  - Ditambahkan: `{', '.join(report.sync_result.added)}`")
    if report.invalid_skills:
        lines.append("\n⚠️ **Skill bermasalah:**")
        for sk in report.invalid_skills[:10]:
            lines.append(f"- `{sk.name}` ({sk.source}): {', '.join(sk.issues)}")
    else:
        lines.append("- Integritas `SKILL.md`: ✅ semua valid")

    if report.stale_skills:
        stale_names = ", ".join(f"{s.name} ({s.age_days}d)" for s in report.stale_skills[:8])
        lines.append(f"- Perlu review (>90 hari tidak disentuh): `{stale_names}`")

    return "\n".join(lines)
