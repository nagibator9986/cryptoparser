from __future__ import annotations

import logging

import httpx

from crypto_monitor.collectors.kgd_rates import DEFAULT_RATES_URL, KgdRatesCollector
from crypto_monitor.models import CryptoRatesSnapshot
from crypto_monitor.storage import SqliteStorage

logger = logging.getLogger(__name__)

# Exact attribution requested by the customer. Must be shown verbatim on every
# rates message; do not paraphrase.
RATES_ATTRIBUTION = (
    "Согласно публикации КГД, стоимость криптовалюты "
    "на основании данных за предыдущие сутки."
)
RATES_TITLE = "Курсы цифровых активов"


def collect_and_store_rates(
    storage: SqliteStorage,
    *,
    url: str = DEFAULT_RATES_URL,
    client: httpx.Client | None = None,
) -> CryptoRatesSnapshot:
    snapshot = KgdRatesCollector(url=url, client=client).fetch()
    storage.save_rates_snapshot(snapshot)
    return snapshot


def get_rates_with_fallback(
    storage: SqliteStorage,
    *,
    url: str = DEFAULT_RATES_URL,
    client: httpx.Client | None = None,
) -> CryptoRatesSnapshot | None:
    """Fetch live KGD rates; on failure fall back to the last stored snapshot.

    The stored snapshot keeps its own data date, so the "за предыдущие сутки"
    attribution stays truthful even when we serve a cached copy.
    """

    try:
        return collect_and_store_rates(storage, url=url, client=client)
    except Exception as exc:
        logger.warning("kgd_rates_fetch_failed_using_cache error=%s", exc)
        return storage.load_latest_rates_snapshot()


def format_amount(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0"
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:,.2f}".replace(",", " ")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def render_rates_plain(snapshot: CryptoRatesSnapshot) -> str:
    lines = [f"{RATES_TITLE} (за {display_date(snapshot.date)})", ""]
    for rate in snapshot.rates:
        usd = f" / ${format_amount(rate.price_usd)}" if rate.price_usd is not None else ""
        lines.append(
            f"{rate.symbol} {rate.name} — {format_amount(rate.price_kzt)} ₸{usd}"
        )
    lines.append("")
    lines.append(RATES_ATTRIBUTION)
    lines.append(snapshot.source_url)
    return "\n".join(lines)


def display_date(iso_date: str) -> str:
    parts = iso_date.split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return iso_date or "—"
