from crypto_monitor.delivery.telegram import split_telegram_segments, unescape_markdown_v2


def test_unescape_markdown_v2() -> None:
    assert unescape_markdown_v2(r"\*Title\* link\.") == "*Title* link."


def test_split_telegram_segments_keeps_limit() -> None:
    chunks = split_telegram_segments(["a" * 10], limit=4)
    assert chunks == ["aaaa", "aaaa", "aa"]
    assert all(len(chunk) <= 4 for chunk in chunks)
