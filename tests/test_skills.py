from pathlib import Path

from crypto_monitor.skills import SkillLoader


def test_skill_loader_lists_existing_skills() -> None:
    loader = SkillLoader(Path("crypto-monitor-skills"))
    names = loader.list_skills()
    assert "crypto-news-classifier" in names
    assert "crypto-digest-quality-check" in names


def test_skill_loader_includes_references() -> None:
    loader = SkillLoader(Path("crypto-monitor-skills"))
    skill = loader.load("crypto-news-classifier", include_references=True)
    assert "REFERENCE: taxonomy.md" in skill.system_prompt
    assert "REFERENCE: geo-priorities.md" in skill.system_prompt
