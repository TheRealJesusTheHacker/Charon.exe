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
        self.keep_objects_seen: list[bool] = []

    def search(self, criteria, keep_objects=False):
        self.criteria_seen.append(criteria)
        self.keep_objects_seen.append(keep_objects)
        if self._error is not None:
            raise self._error
        out = []
        for u in self._updates:
            raw = dict(u)
            if not keep_objects:
                raw.pop("com_update", None)
            out.append(raw)
        return out


class FakeComUpdate:
    """A stand-in WUA IUpdate object, shaped for both seams.

    Carries the COM-style attribute names the search seam reads
    (``Title``, ``DriverModel``, …) and what the download seam needs
    (``RequiresUserAcceptance``, ``AcceptEula``, ``IsDownloaded``,
    ``MaxDownloadSize``). Records every acceptance.
    """

    def __init__(
        self,
        title,
        driver_model="",
        driver_class="",
        provider="",
        version="",
        driver_date=None,
        hardware_ids=(),
        requires_eula=False,
        accept_eula_raises=False,
        max_download_size=None,
        is_downloaded=True,
        is_installed=False,
    ):
        self.Title = title
        self.DriverModel = driver_model
        self.DriverClass = driver_class
        self.DriverManufacturer = provider
        self.DriverVersion = version
        self.DriverVerDate = driver_date
        self.DriverHardwareID = hardware_ids[0] if hardware_ids else ""
        self.hardware_ids = list(hardware_ids)
        self.RequiresUserAcceptance = requires_eula
        self._accept_eula_raises = accept_eula_raises
        self.eula_accepted = False
        self.MaxDownloadSize = max_download_size
        self.IsDownloaded = is_downloaded
        self.IsInstalled = is_installed

    def AcceptEula(self):
        if self._accept_eula_raises:
            raise RuntimeError("fake EULA refusal")
        self.eula_accepted = True

    def raw(self):
        """The raw search mapping this object would arrive as."""
        return {
            "title": self.Title,
            "driver_model": self.DriverModel,
            "driver_class": self.DriverClass,
            "provider": self.DriverManufacturer,
            "version": self.DriverVersion,
            "driver_date": self.DriverVerDate,
            "hardware_ids": list(self.hardware_ids),
            "com_update": self,
        }


class FakeDownloadEntry:
    """One fake IUpdateDownloadResult: a code and an HRESULT."""

    def __init__(self, result_code, hresult=0):
        self.ResultCode = result_code
        self.HResult = hresult


class FakeComSession:
    """A stand-in Microsoft.Update.Session for the WUA seams.

    Serves exactly the downloader machinery (Phase 2) and the
    installer machinery (Phase 4) and records the calls. Any other
    attribute access is a loud failure — a test double must never
    quietly bless an unexpected capability.
    """

    def __init__(
        self,
        codes_by_title=None,
        default_code=2,
        install_codes_by_title=None,
        default_install_code=2,
        reboot_titles=(),
        install_raises_titles=(),
        installer_setup_raises=False,
        claims_success_not_installed=(),
    ):
        self.codes_by_title = dict(codes_by_title or {})
        self.default_code = default_code
        self.downloader_constructed = 0
        self.download_calls: list[list] = []
        # Phase 4: the install side of the house.
        self.install_codes_by_title = dict(install_codes_by_title or {})
        self.default_install_code = default_install_code
        self.reboot_titles = set(reboot_titles)
        self.install_raises_titles = set(install_raises_titles)
        self.installer_setup_raises = installer_setup_raises
        self.claims_success_not_installed = set(
            claims_success_not_installed
        )
        self.installer_constructed = 0
        self.install_calls: list[list] = []

    def CreateUpdateCollection(self):
        return _FakeCollection()

    def CreateUpdateDownloader(self):
        self.downloader_constructed += 1
        return _FakeComDownloader(self)

    def CreateUpdateInstaller(self):
        if self.installer_setup_raises:
            raise RuntimeError("fake installer construction failure")
        self.installer_constructed += 1
        return _FakeComInstaller(self)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected session member requested: {name}")


class _FakeCollection:
    def __init__(self):
        self.items = []

    def Add(self, item):
        self.items.append(item)


class _FakeDownloadResult:
    """The result object the real Download() returns (IDownloadResult).

    Per-update verdicts live HERE in the real API — the downloader
    itself has no GetUpdateResult method. (The kit hunt of
    2026-10-20 found the fake previously served verdicts from the
    downloader, mirroring the seam's mistaken shape and hiding that
    every real download would be reported failed.)
    """

    def __init__(self, session, downloader):
        self._session = session
        self._downloader = downloader

    def GetUpdateResult(self, index):
        updates = list(self._downloader.Updates.items) if self._downloader.Updates else []
        title = updates[index].Title
        code = self._session.codes_by_title.get(
            title, self._session.default_code
        )
        hresult = 0 if code in (2, 3) else -2145124300
        return FakeDownloadEntry(code, hresult)


class _FakeComDownloader:
    """The downloader half of FakeComSession (records; serves codes).

    Shaped like the documented IUpdateDownloader: ``Download()``
    takes no argument and returns a :class:`_FakeDownloadResult`.
    Any other member — GetUpdateResult included — is a loud failure,
    exactly the discipline a test double owes the real API.
    """

    def __init__(self, session):
        self._session = session
        self.Updates = None

    def Download(self):
        updates = list(self.Updates.items) if self.Updates else []
        self._session.download_calls.append(updates)
        return _FakeDownloadResult(self._session, self)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected downloader member requested: {name}")


class _FakeInstallResult:
    """One fake IInstallationResult for a single-update install."""

    def __init__(self, session, update):
        self._session = session
        self._update = update
        title = update.Title if update is not None else ""
        self._code = session.install_codes_by_title.get(
            title, session.default_install_code
        )
        self.ResultCode = self._code
        self.RebootRequired = title in session.reboot_titles
        self.HResult = 0 if self._code in (2, 3) else -2145124300

    def GetUpdateResult(self, index):
        assert index == 0, "one update per install — no other index exists"
        return FakeDownloadEntry(self._code, self.HResult)


class _FakeComInstaller:
    """The installer half of FakeComSession (records; serves codes).

    Installs exactly what its ``Updates`` collection holds — the
    seam under test is expected to hand it one update at a time,
    and the recorded calls let tests prove it.
    """

    def __init__(self, session):
        self._session = session
        self.Updates = None

    def Install(self):
        updates = list(self.Updates.items) if self.Updates else []
        self._session.install_calls.append(updates)
        update = updates[0] if updates else None
        title = update.Title if update is not None else ""
        if title in self._session.install_raises_titles:
            raise RuntimeError("fake mid-install COM failure")
        result = _FakeInstallResult(self._session, update)
        if (
            update is not None
            and result.ResultCode in (2, 3)
            and title not in self._session.claims_success_not_installed
        ):
            update.IsInstalled = True
        return result

    def __getattr__(self, name):
        raise AssertionError(f"unexpected installer member requested: {name}")
