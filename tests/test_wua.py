"""WUA seam: the exact search criteria, result pass-through, and the
designed failure when Windows Update can't be asked."""

import pytest

from charon.wua import WUA_SEARCH_CRITERIA, WuaUnavailable, search_driver_updates
from conftest import FakeSearcher


def test_search_uses_exact_driver_criteria(wua_updates):
    searcher = FakeSearcher(updates=wua_updates)
    results = search_driver_updates(searcher)
    assert searcher.criteria_seen == ["IsInstalled=0 and Type='Driver'"]
    assert WUA_SEARCH_CRITERIA == "IsInstalled=0 and Type='Driver'"
    assert len(results) == 7
    assert results[0]["driver_model"] == "Intel(R) Wi-Fi 6 AX201 160MHz"


def test_search_failure_is_designed_error():
    searcher = FakeSearcher(error=RuntimeError("COM exploded"))
    with pytest.raises(WuaUnavailable) as excinfo:
        search_driver_updates(searcher)
    message = excinfo.value.user_message
    assert "Windows Update didn't answer" in message
    assert "Traceback" not in message
    # The technical cause is preserved for the log, not the screen.
    assert "COM exploded" in excinfo.value.detail


def test_no_searcher_on_non_windows(monkeypatch):
    import charon.wua as wua

    monkeypatch.setattr(wua.sys, "platform", "linux")
    with pytest.raises(WuaUnavailable):
        search_driver_updates(None)
