from __future__ import annotations

from pathlib import Path

import yaml

from crypto_monitor.models import SourceConfig


def load_sources(path: Path, include_disabled: bool = False) -> list[SourceConfig]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("sources", data if isinstance(data, list) else [])
    return [
        SourceConfig.model_validate(item)
        for item in sources
        if include_disabled or item.get("enabled", True)
    ]
