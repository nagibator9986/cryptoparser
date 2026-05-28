from __future__ import annotations

from abc import ABC, abstractmethod

from crypto_monitor.models import RawArticle, SourceConfig


class Collector(ABC):
    @abstractmethod
    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        """Collect raw articles from a source."""
