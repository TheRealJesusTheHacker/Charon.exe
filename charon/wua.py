"""CHARON.EXE — Windows Update Agent seam (SEARCH ONLY).

This module asks Windows Update one question — "which driver updates
do you offer for this PC?" — and nothing else. It creates a searcher,
runs one search, reads the results, and stops.

There is intentionally **no** capability here to fetch or apply an
update: no downloader object, no installer object, no such methods on
any class in this module. A search may carry the opaque update object
(``com_update``) so the Phase 2 download seam can revalidate and
fetch the very thing Windows Update just offered — carrying a handle
is not fetching, and the fetching itself lives only in
``wua_download``. The test suite pins this module's API surface.
"""

from __future__ import annotations

import logging
import sys
from typing import Protocol

log = logging.getLogger("charon.wua")

# The exact search criteria: driver updates that are not installed yet.
# Pinned by tests — changing what Charon asks Windows Update is a
# deliberate, reviewable act.
WUA_SEARCH_CRITERIA = "IsInstalled=0 and Type='Driver'"


class WuaUnavailable(Exception):
    """Windows Update could not be asked (designed failure).

    Plain-language message for the user; technical detail goes to the
    log. A scan can still proceed without WUA — every device simply
    classifies ``unknown``, because Charon never guesses.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class UpdateSearcher(Protocol):
    """The seam: anything that can run one WUA search.

    Implementations return raw update mappings (title, driver_model,
    driver_class, provider, version, driver_date_text/driver_date,
    hardware_ids). Normalization happens in ``inventory``. With
    ``keep_objects=True`` each mapping also carries the opaque seam
    object under ``com_update`` — that is how the Phase 2 download
    seam revalidates against a fresh search and downloads the very
    object Windows Update just offered, never a stale handle.
    """

    def search(self, criteria: str, keep_objects: bool = False) -> list[dict]:
        ...


def _com_field(obj, name: str, default=""):
    """Read one COM property defensively — any may be absent."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


class ComUpdateSearcher:
    """The real Windows searcher, backed by the WUA COM session.

    Only ``Search`` is ever called. The session is used read-only.
    """

    def __init__(self, session=None):
        if session is None:
            import win32com.client  # pywin32, Windows-only by design

            session = win32com.client.Dispatch("Microsoft.Update.Session")
        self._session = session

    def search(self, criteria: str, keep_objects: bool = False) -> list[dict]:
        searcher = self._session.CreateUpdateSearcher()
        result = searcher.Search(criteria)
        updates: list[dict] = []
        for update in result.Updates:
            hardware_id = _com_field(update, "DriverHardwareID")
            raw = {
                "title": str(_com_field(update, "Title")),
                "driver_model": str(_com_field(update, "DriverModel")),
                "driver_class": str(_com_field(update, "DriverClass")),
                "provider": str(
                    _com_field(update, "DriverManufacturer")
                ),
                "version": str(_com_field(update, "DriverVersion")),
                "driver_date": _com_field(update, "DriverVerDate", None),
                "hardware_ids": [hardware_id] if hardware_id else [],
            }
            if keep_objects:
                # The opaque seam object, carried for the download
                # seam's revalidation. It is never serialized, never
                # normalized into the report model — the manifest
                # stays data; the handle stays a handle.
                raw["com_update"] = update
            updates.append(raw)
        return updates


def search_driver_updates(
    searcher: UpdateSearcher | None = None, keep_objects: bool = False
) -> list[dict]:
    """Return raw driver-update mappings offered by Windows Update.

    Raises :class:`WuaUnavailable` (plain-language) when Windows Update
    cannot be asked — a designed state the caller degrades around,
    never a traceback for the user.
    """
    if searcher is None:
        if not sys.platform.startswith("win"):
            raise WuaUnavailable(
                "Windows Update can only be asked on a Windows PC.",
                detail=f"sys.platform={sys.platform}",
            )
        try:
            searcher = ComUpdateSearcher()
        except Exception as exc:
            raise WuaUnavailable(
                "Charon couldn't reach Windows Update on this PC.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
    try:
        return searcher.search(WUA_SEARCH_CRITERIA, keep_objects=keep_objects)
    except Exception as exc:
        log.warning("WUA search failed: %s", exc)
        raise WuaUnavailable(
            "Windows Update didn't answer. Your drivers can still be "
            "listed, but Charon can't tell which ones are outdated "
            "until Windows Update responds.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
