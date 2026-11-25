"""Auto-sync Hermes custom and curated skills into Agy's global skills directory."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
DEFAULT_AGY_SKILLS_DIR = Path.home() / ".gemini" / "config" / "skills"

EXCLUDED_CATEGORIES = frozenset({"apple", "email", "media", "social-media", "note-taking"})
EXCLUDED_SKILLS = frozenset(
    {
        "architecture-diagram-renderer",
        "cloud-agent-sandboxes",
        "deepseek-harness",
        "godmode",
        "hermes-agent",
        "hermes-agent-skill-authoring",
        "inspecting-hermes-desktop-dom",
        "kde-plasma-theming",
        "linux-desktop-gaming",
        "refurbished-laptop-vetting",
        "sdlc-review",
    }
)
BUNDLED_ALLOWLIST = frozenset(
    {
        "architecture-diagram",
        "codebase-inspection",
        "github",
        "llm-wiki",
        "node-inspect-debugger",
        "pdf",
        "python-debugpy",
        "requesting-code-review",
        "simplify-code",
        "systematic-debugging",
        "test-driven-development",
        "xlsx",
    }
)


@dataclass
class SkillSyncResult:
    """Summary of a Hermes -> Agy skill symlink sync pass."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    total_linked: int = 0


def _read_bundled_names(hermes_skills_dir: Path) -> set[str]:
    manifest = hermes_skills_dir / ".bundled_manifest"
    if not manifest.is_file():
        return set()
    names: set[str] = set()
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and ":" in stripped:
                names.add(stripped.split(":", 1)[0].strip())
    except OSError:
        return set()
    return names


def _discover_sync_targets(hermes_skills_dir: Path) -> dict[str, Path]:
    bundled = _read_bundled_names(hermes_skills_dir)
    targets: dict[str, Path] = {}

    def _consider(skill_dir: Path) -> None:
        if not (skill_dir / "SKILL.md").is_file():
            return
        name = skill_dir.name
        if name.startswith(".") or name in EXCLUDED_SKILLS:
            return
        if name not in bundled or name in BUNDLED_ALLOWLIST:
            targets.setdefault(name, skill_dir)

    try:
        entries = sorted(hermes_skills_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return targets

    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            _consider(entry)
            continue
        if entry.name in EXCLUDED_CATEGORIES:
            continue
        try:
            sub_entries = sorted(entry.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for sub in sub_entries:
            if not sub.name.startswith(".") and sub.is_dir():
                _consider(sub)

    return targets


def _points_into_dir(symlink_path: Path, parent_dir: Path) -> bool:
    try:
        raw_target = os.readlink(symlink_path)
    except OSError:
        return False
    target_abs = Path(raw_target)
    if not target_abs.is_absolute():
        target_abs = symlink_path.parent / target_abs
    try:
        norm_target = Path(os.path.normpath(str(target_abs)))
        norm_parent = Path(os.path.normpath(str(parent_dir)))
        norm_target.relative_to(norm_parent)
        return True
    except ValueError:
        return False


def sync_hermes_skills(
    hermes_skills_dir: Path = DEFAULT_HERMES_SKILLS_DIR,
    agy_skills_dir: Path = DEFAULT_AGY_SKILLS_DIR,
) -> SkillSyncResult:
    """Ensure all non-excluded custom & curated Hermes skills are symlinked into Agy."""
    result = SkillSyncResult()
    if not hermes_skills_dir.is_dir():
        return result

    agy_skills_dir.mkdir(parents=True, exist_ok=True)
    desired = _discover_sync_targets(hermes_skills_dir)

    # Prune dead or uninvited symlinks that we manage (pointing inside hermes_skills_dir).
    try:
        existing = list(agy_skills_dir.iterdir())
    except OSError:
        return result

    for item in existing:
        if not item.is_symlink():
            continue
        if not _points_into_dir(item, hermes_skills_dir):
            continue
        target = desired.get(item.name)
        if target is None or not item.exists() or item.resolve() != target.resolve():
            try:
                item.unlink()
                result.removed.append(item.name)
            except OSError:
                pass

    # Link missing or newly added skills without touching real local directories.
    for name, src in desired.items():
        dst = agy_skills_dir / name
        if dst.exists() or dst.is_symlink():
            continue
        try:
            dst.symlink_to(src)
            result.added.append(name)
        except OSError:
            pass

    result.total_linked = sum(
        1
        for item in agy_skills_dir.iterdir()
        if item.is_symlink() and _points_into_dir(item, hermes_skills_dir) and item.exists()
    )
    return result


if __name__ == "__main__":
    res = sync_hermes_skills()
    print(
        f"Synced Hermes -> Agy skills: total={res.total_linked}, "
        f"added={res.added}, removed={res.removed}"
    )
