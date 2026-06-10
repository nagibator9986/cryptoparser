from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crypto_monitor.collectors.kgd_rates import (
    APPROVED_COINS,
    KgdRatesCollector,
    parse_rates_html,
)
from crypto_monitor.config import Settings
from crypto_monitor.delivery.telegram import TelegramDelivery, render_rates_markdown_v2
from crypto_monitor.models import CryptoRate, CryptoRatesSnapshot
from crypto_monitor.rates import (
    RATES_ATTRIBUTION,
    format_amount,
    get_rates_with_fallback,
    render_rates_plain,
)
from crypto_monitor.storage import SqliteStorage

# Mirrors the real qoldau table: nbsp thousands, comma decimals, two dates,
# Dash with no "(SYM)" suffix, Siacoin rounded to 0, plus a non-approved row
# (AAVE) that must be ignored.
FIXTURE_HTML = (
    "<table>"
    "<tr><th>Криптовалюта</th><th>Дата</th><th>Курс USD/KZT</th>"
    "<th>Средняя стоимость (KZT)</th><th>Рыночная капитализация</th>"
    "<th>Объем торгов</th></tr>"
    "<tr><td>Bitcoin (BTC)</td><td>03.06.2026</td><td>493,150</td>"
    "<td>32\xa0632\xa0058,050</td><td>1\xa0337\xa0294\xa0531\xa0019,200</td>"
    "<td>54\xa0597\xa0857\xa0504,730</td></tr>"
    "<tr><td>Dash</td><td>03.06.2026</td><td>493,150</td>"
    "<td>11\xa0000,500</td><td>1\xa0000,000</td><td>500,000</td></tr>"
    "<tr><td>Siacoin (SC)</td><td>03.06.2026</td><td>493,150</td>"
    "<td>00,000</td><td>47\xa0280\xa0492,990</td><td>4\xa0621\xa0005,490</td></tr>"
    "<tr><td>AAVE (Aave)</td><td>03.06.2026</td><td>493,150</td>"
    "<td>36\xa0700,320</td><td>1\xa0156\xa0479\xa0788,170</td>"
    "<td>371\xa0414\xa0775,240</td></tr>"
    "<tr><td>Bitcoin (BTC)</td><td>02.06.2026</td><td>488,960</td>"
    "<td>33\xa0935\xa0970,800</td><td>1\xa0399\xa0319\xa0938\xa0277,410</td>"
    "<td>45\xa0645\xa0887\xa0649,590</td></tr>"
    "</table>"
)


def test_parse_rates_html_selects_latest_day_and_approved_coins() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML, source_url="https://token.qoldau.kz/x")

    assert snapshot.date == "2026-06-03"
    assert snapshot.usd_kzt == 493.15

    symbols = {rate.symbol for rate in snapshot.rates}
    # Only the three approved coins present in the fixture; AAVE is dropped.
    assert symbols == {"BTC", "DASH", "SC"}

    btc = next(rate for rate in snapshot.rates if rate.symbol == "BTC")
    assert btc.price_kzt == 32_632_058.05
    assert btc.price_usd is not None
    assert round(btc.price_usd, 2) == round(32_632_058.05 / 493.15, 2)


def test_parse_rates_html_matches_dash_without_symbol_suffix() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML)
    dash = next(rate for rate in snapshot.rates if rate.symbol == "DASH")
    assert dash.name == "Dash"
    assert dash.price_kzt == 11_000.5


def test_parse_rates_html_keeps_zero_rounded_price_faithfully() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML)
    sc = next(rate for rate in snapshot.rates if rate.symbol == "SC")
    # KGD rounds sub-cent coins to 0.000 KZT; we report the official value.
    assert sc.price_kzt == 0.0


def test_parse_rates_html_preserves_approved_order() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML)
    order = [rate.symbol for rate in snapshot.rates]
    approved_order = [coin.symbol for coin in APPROVED_COINS]
    assert order == [s for s in approved_order if s in set(order)]


def test_format_amount_thresholds() -> None:
    assert format_amount(32_632_058.05) == "32 632 058"
    assert format_amount(11.5) == "11.50"
    assert format_amount(0) == "0"
    assert format_amount(None) == "—"
    assert format_amount(0.0034) == "0.0034"


def test_render_markdown_v2_has_attribution_and_escapes_specials() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML)
    text = render_rates_markdown_v2(snapshot)
    # Attribution present, MarkdownV2 dots escaped.
    assert "за предыдущие сутки" in text
    assert "03\\.06\\.2026" in text
    assert "`BTC`" in text
    # Dynamic numeric/currency values must be escaped too: the KZT/USD separator
    # pipe and the price dots/spaces on the Dash line (11 000.50 KZT).
    assert "11 000 ₸" in text  # space-grouped, escaped
    assert "\\|" in text  # KZT | USD separator is an escaped pipe


def test_parse_rates_html_empty_when_markup_changes() -> None:
    # No approved-coin rows (e.g. qoldau changed columns or only lists others).
    changed = (
        "<table><tr><th>Crypto</th><th>Date</th><th>Rate</th><th>Avg</th>"
        "<th>Cap</th><th>Vol</th></tr>"
        "<tr><td>AAVE (Aave)</td><td>03.06.2026</td><td>493,150</td>"
        "<td>36 700,320</td><td>1,0</td><td>1,0</td></tr></table>"
    )
    snapshot = parse_rates_html(changed)
    assert snapshot.rates == []
    assert snapshot.date == "2026-06-03"  # date still read, but no approved coins


def test_collector_fetch_raises_when_no_approved_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<table></table>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        KgdRatesCollector(url="https://token.qoldau.kz/x", client=client).fetch()


def test_parse_rates_html_skips_unparseable_price_but_keeps_real_zero() -> None:
    html = (
        "<table><tr><th>c</th><th>d</th><th>r</th><th>avg</th><th>cap</th><th>v</th></tr>"
        # BTC price cell is garbage -> skipped (not published as 0)
        "<tr><td>Bitcoin (BTC)</td><td>03.06.2026</td><td>493,150</td>"
        "<td>n/a</td><td>1,0</td><td>1,0</td></tr>"
        # SC genuine 0.000 -> kept
        "<tr><td>Siacoin (SC)</td><td>03.06.2026</td><td>493,150</td>"
        "<td>00,000</td><td>1,0</td><td>1,0</td></tr></table>"
    )
    snapshot = parse_rates_html(html)
    symbols = {r.symbol for r in snapshot.rates}
    assert "BTC" not in symbols  # unparseable price -> dropped, not faked as 0
    assert "SC" in symbols
    assert next(r for r in snapshot.rates if r.symbol == "SC").price_kzt == 0.0


def test_render_plain_has_attribution() -> None:
    snapshot = parse_rates_html(FIXTURE_HTML)
    text = render_rates_plain(snapshot)
    assert RATES_ATTRIBUTION in text
    assert "BTC Bitcoin" in text


def test_storage_rates_roundtrip(tmp_path: Path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    assert storage.load_latest_rates_snapshot() is None
    older = CryptoRatesSnapshot(
        date="2026-06-02",
        usd_kzt=488.96,
        rates=[CryptoRate(symbol="BTC", name="Bitcoin", price_kzt=1.0)],
        source_url="https://token.qoldau.kz/x",
    )
    newer = parse_rates_html(FIXTURE_HTML)
    storage.save_rates_snapshot(older)
    storage.save_rates_snapshot(newer)
    latest = storage.load_latest_rates_snapshot()
    assert latest is not None
    assert latest.date == "2026-06-03"


def test_get_rates_with_fallback_uses_cache_on_failure(tmp_path: Path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    storage.save_rates_snapshot(parse_rates_html(FIXTURE_HTML))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = get_rates_with_fallback(storage, url="https://token.qoldau.kz/x", client=client)
    assert snapshot is not None
    assert snapshot.date == "2026-06-03"  # served from cache


def test_collector_fetch_parses_live_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FIXTURE_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = KgdRatesCollector(url="https://token.qoldau.kz/x", client=client).fetch()
    assert snapshot.date == "2026-06-03"
    assert {rate.symbol for rate in snapshot.rates} == {"BTC", "DASH", "SC"}


def test_send_rates_posts_markdown_v2(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="123",
        CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = parse_rates_html(FIXTURE_HTML)

    TelegramDelivery(settings, client=client).send_rates(snapshot, chat_id="123")

    assert "sendMessage" in captured["url"]
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["chat_id"] == "123"
    assert "за предыдущие сутки" in payload["text"]
