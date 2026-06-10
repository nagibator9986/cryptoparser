from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from crypto_monitor.collectors.rss import build_http_client
from crypto_monitor.models import CryptoRate, CryptoRatesSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoinSpec:
    symbol: str  # display symbol, e.g. "BTC"
    filter_value: str  # value of the qoldau `flCryptoCurrencyType` option
    ru_name: str  # human label for the digest
    match_tokens: tuple[str, ...] = field(default_factory=tuple)


# The official KGD list of digital assets whose value is published daily for
# digital-mining-fee purposes. Order here is the order shown in the digest.
# `filter_value` matches the qoldau dropdown verbatim (note the Doge casing).
# `match_tokens` are lowercase fragments used to bind a table row to a coin;
# Dash is the only entry whose table name has no "(SYM)" suffix.
APPROVED_COINS: tuple[CoinSpec, ...] = (
    CoinSpec("BTC", "BTC", "Bitcoin", ("(btc)",)),
    CoinSpec("ETC", "ETC", "Ethereum Classic", ("(etc)",)),
    CoinSpec("BCH", "BCH", "Bitcoin Cash", ("(bch)",)),
    CoinSpec("LTC", "LTC", "Litecoin", ("(ltc)",)),
    CoinSpec("XMR", "XMR", "Monero", ("(xmr)",)),
    CoinSpec("ZEC", "ZEC", "Zcash", ("(zec)",)),
    CoinSpec("DASH", "DASH", "Dash", ("dash",)),
    CoinSpec("TRX", "TRX", "Tron", ("(trx)",)),
    CoinSpec("DOGE", "Doge", "Dogecoin", ("(doge)",)),
    CoinSpec("ZEN", "ZEN", "Horizen", ("(zen)",)),
    CoinSpec("SC", "SC", "Siacoin", ("(sc)",)),
)

DEFAULT_RATES_URL = "https://token.qoldau.kz/ru/references/crypto-currency/list"


class KgdRatesCollector:
    """Fetches the KGD-published approved-coin prices from the qoldau portal.

    qoldau exposes no JSON API and no export — only a server-rendered HTML
    table with a multiselect currency filter and a date range. We request the
    11 approved coins in one GET (repeated `flCryptoCurrencyType` params) and
    parse the most recent date present, which is the previous day (T-1).
    """

    def __init__(
        self,
        url: str = DEFAULT_RATES_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._client = client or build_http_client(timeout)

    def fetch(self) -> CryptoRatesSnapshot:
        params = [("flCryptoCurrencyType", coin.filter_value) for coin in APPROVED_COINS]
        response = self._client.get(self.url, params=params)
        response.raise_for_status()
        snapshot = parse_rates_html(response.text, source_url=self.url)
        if not snapshot.rates:
            raise ValueError("KGD rates page returned no approved-coin rows")
        return snapshot


def parse_rates_html(html: str, *, source_url: str = DEFAULT_RATES_URL) -> CryptoRatesSnapshot:
    """Parse the qoldau crypto-currency table into the latest-day snapshot.

    Pure function (no network) so it can be unit-tested against a fixture.
    Selects the most recent date in the table and returns one row per approved
    coin in `APPROVED_COINS` order. Missing coins are skipped, not faked.
    """

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    rows = _extract_data_rows(table)
    if not rows:
        return CryptoRatesSnapshot(date="", usd_kzt=0.0, rates=[], source_url=source_url)

    latest_iso = max(row["date_iso"] for row in rows if row["date_iso"])
    latest_rows = [row for row in rows if row["date_iso"] == latest_iso]
    usd_kzt = next((row["usd_kzt"] for row in latest_rows if row["usd_kzt"]), 0.0)

    rates: list[CryptoRate] = []
    for coin in APPROVED_COINS:
        row = _match_row(coin, latest_rows)
        if row is None:
            logger.info("kgd_rate_missing symbol=%s date=%s", coin.symbol, latest_iso)
            continue
        price_kzt = row["price_kzt"]
        if price_kzt is None:
            logger.info("kgd_rate_unparseable symbol=%s date=%s", coin.symbol, latest_iso)
            continue
        price_usd = price_kzt / usd_kzt if usd_kzt else None
        rates.append(
            CryptoRate(
                symbol=coin.symbol,
                name=coin.ru_name,
                price_kzt=price_kzt,
                price_usd=price_usd,
                market_cap_kzt=row["market_cap_kzt"],
                volume_kzt=row["volume_kzt"],
            )
        )
    return CryptoRatesSnapshot(
        date=latest_iso,
        usd_kzt=usd_kzt,
        rates=rates,
        source_url=source_url,
    )


def _extract_data_rows(table: object) -> list[dict]:
    if table is None:
        return []
    parsed: list[dict] = []
    for tr in table.find_all("tr"):  # type: ignore[attr-defined]
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 6:
            continue
        date_iso = _parse_kz_date(cells[1])
        if date_iso is None:
            continue  # header row or malformed line
        parsed.append(
            {
                "name": cells[0],
                "name_lower": cells[0].lower(),
                "date_iso": date_iso,
                "usd_kzt": _parse_kz_decimal(cells[2]),
                # None means unparseable (distinct from a real 0.000, which KGD
                # publishes for sub-cent coins like Siacoin and we keep).
                "price_kzt": _parse_kz_decimal(cells[3]),
                "market_cap_kzt": _parse_kz_decimal(cells[4]),
                "volume_kzt": _parse_kz_decimal(cells[5]),
            }
        )
    return parsed


def _match_row(coin: CoinSpec, rows: list[dict]) -> dict | None:
    for row in rows:
        if any(token in row["name_lower"] for token in coin.match_tokens):
            return row
    return None


def _parse_kz_decimal(value: str) -> float | None:
    """Parse qoldau number format: nbsp/space thousands, comma decimals."""

    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_kz_date(value: str) -> str | None:
    text = value.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None
