from catalyst_radar.services.telegram_ui import (
    alert_kb,
    detail_kb,
    dismissed_kb,
    menu_kb,
)


def _labels(kb: dict) -> list[str]:
    return [b["text"] for row in kb["inline_keyboard"] for b in row]


def _datas(kb: dict) -> list[str | None]:
    return [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]


def test_alert_kb_ipo_has_star_and_dismiss() -> None:
    kb = alert_kb(event_id=9, event_type="ipo", source_url="http://x")
    assert "⭐ Star" in _labels(kb)
    assert "sr:9" in _datas(kb)
    assert "dx:9" in _datas(kb)
    assert "☰ Menu" in _labels(kb)


def test_alert_kb_starred_label_flips() -> None:
    kb = alert_kb(event_id=9, event_type="ipo", starred=True)
    assert "★ Starred ✓" in _labels(kb)
    assert "⭐ Star" not in _labels(kb)


def test_alert_kb_non_ipo_is_legacy() -> None:
    kb = alert_kb(event_id=9, event_type="earnings", source_url="http://x")
    assert "sr:9" not in _datas(kb)
    assert "🔗 Source / prospectus" in _labels(kb)
    assert "☰ Menu" in _labels(kb)


def test_alert_kb_without_event_id_is_legacy() -> None:
    kb = alert_kb(source_url="http://x")
    assert all((d or "").startswith(("m", "http")) or d is None for d in _datas(kb))
    assert "🔗 Source / prospectus" in _labels(kb)


def test_dismissed_kb_is_single_undo() -> None:
    assert _datas(dismissed_kb(5)) == ["un:5"]


def test_menu_kb_has_starred_entry() -> None:
    assert "v:star:0" in _datas(menu_kb())


def test_detail_kb_ipo_uses_sd_token() -> None:
    kb = detail_kb("ipo", event_id=3, event_type="ipo", starred=False)
    assert "sd:3:ipo" in _datas(kb)
    assert "⭐ Star" in _labels(kb)


def test_detail_kb_non_ipo_has_no_star() -> None:
    kb = detail_kb("earnings", event_id=3, event_type="earnings")
    assert "sd:3" not in _datas(kb)
