from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    directory: Path
    system_prompt: str


class SkillNotFoundError(FileNotFoundError):
    """Raised when a skill directory or SKILL.md cannot be found."""


class SkillLoader:
    """Loads prompt-based skills and their local references/assets."""

    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root
        # Skill files are static for the lifetime of a process. The pipeline
        # loads the same handful of skills once per article, so caching the
        # assembled system prompt removes thousands of disk reads and string
        # joins per run.
        self._cache: dict[tuple[str, bool, bool], Skill] = {}
        self._cache_lock = threading.Lock()

    def load(
        self,
        skill_name: str,
        include_references: bool = True,
        include_assets: bool = False,
    ) -> Skill:
        cache_key = (skill_name, include_references, include_assets)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        skill = self._load_uncached(skill_name, include_references, include_assets)
        with self._cache_lock:
            self._cache[cache_key] = skill
        return skill

    def _load_uncached(
        self,
        skill_name: str,
        include_references: bool,
        include_assets: bool,
    ) -> Skill:
        skill_dir = self.skills_root / skill_name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            raise SkillNotFoundError(f"Skill not found: {skill_file}")

        parts = [skill_file.read_text(encoding="utf-8")]

        if include_references:
            references_dir = skill_dir / "references"
            if references_dir.exists():
                for reference in sorted(references_dir.glob("*.md")):
                    parts.append(
                        self._section(
                            f"REFERENCE: {reference.name}",
                            reference.read_text("utf-8"),
                        )
                    )

        if include_assets:
            assets_dir = skill_dir / "assets"
            if assets_dir.exists():
                for asset in sorted(assets_dir.iterdir()):
                    if asset.is_file():
                        parts.append(
                            self._section(
                                f"ASSET: {asset.name}",
                                asset.read_text("utf-8"),
                            )
                        )

        return Skill(name=skill_name, directory=skill_dir, system_prompt="\n".join(parts))

    def list_skills(self) -> list[str]:
        if not self.skills_root.exists():
            return []
        return sorted(
            path.name
            for path in self.skills_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        )

    @staticmethod
    def _section(title: str, body: str) -> str:
        return f"\n\n---\n# {title}\n\n{body}\n"


def build_user_payload(task: str, data: dict) -> str:
    return (
        f"{task}\n"
        "Return exactly one valid JSON object. "
        "Do not include Markdown fences, comments, or prose.\n\n"
        f"Input:\n{json.dumps(data, ensure_ascii=False, indent=2, default=str)}"
    )
