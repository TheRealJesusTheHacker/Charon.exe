"""Shared fixtures and fakes for the Charon Phase 1 test suite.

Everything runs on Linux against recorded fixtures: the fakes stand in
for the two Windows seams (the subprocess runner behind
``platform_inventory`` and the searcher behind ``wua``) and record
exactly what Charon asks the OS to do, so the safety tests can pin
the commands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from charon.platform_inventory import InventoryUnavailable  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BS = chr(92)  # one backslash, for building Windows-style IDs safely


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str):
    return json.loads(load_fixture(name))


@pytest.fixture(scope="session")
def cim_text() -> str:
    return load_fixture("cim_drivers.json")


@pytest.fixture(scope="session")
def cim_entries() -> list:
    return load_json_fixture("cim_drivers.json")


@pytest.fixture(scope="session")
def pnputil_text() -> str:
    return load_fixture("pnputil_drivers.txt")


@pytest.fixture(scope="session")
def wua_updates() -> list:
    return load_json_fixture("wua_updates.json")


class RecordingRunner:
    """Fake for platform_inventory's runner seam.

    Serves fixture text per command and records every argv. Refuses
    (loudly) any command it doesn't recognize — a test double must
    never quietly bless an unexpected OS call.
    """

    def __init__(self, cim_text="", pnputil_text="", fail=()):
        self.cim_text = cim_text
        self.pnputil_text = pnputil_text
        self.fail = set(fail)  # subset of {"powershell", "pnputil"}
        self.calls: list[list[str]] = []

    def run(self, args):
        args = list(args)
        self.calls.append(args)
        tool = args[0]
        if tool in self.fail:
            raise InventoryUnavailable(
                "fake failure", detail=f"{tool} was told to fail"
            )
        if tool == "powershell":
            return self.cim_text
        if tool == "pnputil":
            return self.pnputil_text
        raise AssertionError(f"unexpected command from Charon: {args!r}")


class FakeSearcher:
    """Fake for wua's searcher seam; records the criteria it was given."""

    def __init__(self, updates=None, error=None):
        self._updates = updates or []
        self._error = error
        self.criteria_seen: list[str] = []

    def search(self, criteria):
        self.criteria_seen.append(criteria)
        if self._error is not None:
            raise self._error
        return [dict(u) for u in self._updates]
