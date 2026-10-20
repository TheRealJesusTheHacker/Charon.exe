"""CHARON.EXE — Windows Update Agent seam (DOWNLOAD ONLY).

This is the one module in the package that may ask Windows Update to
fetch an update. It creates a session and a downloader, downloads a
batch of updates that a fresh search already returned, and reports the
per-update outcome honestly. It contains **no** installer capability
of any kind: downloading stages an update in the Windows Update
cache — nothing is installed, applied, or offered to a device here.
Phase 4 will own installing, behind the Phase 3 toll, in its own
seam, when its time comes.

Staging truth, said plainly: ``Download()`` stages updates into the
Windows Update download cache on the system drive. There is **no**
arbitrary target folder — Windows Update does not offer one. Charon's
own download ledger (``download.py``) records what is aboard; the
cache belongs to Windows.

EULA law for this seam: some updates require the user to accept
license terms before Windows Update will fetch them. When an update
requires acceptance, this seam accepts the terms explicitly and the
fact is **recorded per update** in the returned results (and so lands
in the ledger). Silence about an acceptance would be a lie by
omission; Charon doesn't tell those.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .download import (
    EVENT_UPDATE_PROGRESS,
    ProgressEvent,
)
from .wua import WuaUnavailable  # noqa: F401  (re-exported for callers)

log = logging.getLogger("charon.wua_download")

# WUA IUpdateDownloadResult ResultCode values
# (OperationResultCode): the per-update verdicts Windows Update
# reports after a Download() call returns.
ORC_NOT_STARTED = 0
ORC_IN_PROGRESS = 1
ORC_SUCCEEDED = 2
ORC_SUCCEEDED_WITH_ERRORS = 3
ORC_FAILED = 4
ORC_ABORTED = 5

# Charon's own result vocabulary (also the ledger's).
RESULT_SUCCEEDED = "succeeded"
RESULT_SUCCEEDED_WITH_ERRORS = "succeeded_with_errors"
RESULT_FAILED = "failed"
RESULT_ABORTED = "aborted"


class WuaDownloadUnavailable(Exception):
    """Windows Update could not be asked to fetch (designed failure).

    Plain-language message for the user; technical detail goes to the
    log. Distinct from a per-update failure: this means the download
    could not even begin.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class DownloadResult:
    """The honest outcome for one update in a download batch."""

    index: int  # position in the batch, matching the input order
    status: str  # one of the RESULT_* constants
    result_code: int | None = None  # raw WUA code, for the log/ledger
    hresult: int | None = None  # raw WUA error, when present
    eula_accepted: bool = False
    size_bytes: int | None = None  # WUA's reported download size, if any
    detail: str = ""


class UpdateDownloader(Protocol):
    """The seam: anything that can stage a batch of WUA updates.

    ``updates`` are opaque WUA update objects exactly as a search
    returned them (never reconstructed, never substituted).
    ``emit`` — when provided — receives intermediate progress events
    only; started/completed bookkeeping belongs to the caller.
    Implementations return one :class:`DownloadResult` per input
    update, in the same order.
    """

    def download(
        self,
        updates: list[Any],
        emit: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadResult]:
        ...


def _com_field(obj, name: str, default=None):
    """Read one COM property defensively — any may be absent."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _size_bytes(value) -> int | None:
    """WUA's reported download size, sanitized for the ledger.

    Only a finite, non-negative number means anything; NaN would
    raise in ``int()``, infinity likewise, and a negative size is
    nonsense — all three come back ``None`` ("WUA didn't say")
    instead of crashing the batch or poisoning the record.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    size = int(value)
    return size if size >= 0 else None


def _status_for_code(code: int) -> tuple[str, str]:
    """Map a WUA result code to Charon's status + plain-language note."""
    if code == ORC_SUCCEEDED:
        return RESULT_SUCCEEDED, ""
    if code == ORC_SUCCEEDED_WITH_ERRORS:
        return (
            RESULT_SUCCEEDED_WITH_ERRORS,
            "Windows Update fetched this driver, but reported errors "
            "along the way — it is staged, and Charon will flag it "
            "again before anything is applied.",
        )
    if code == ORC_FAILED:
        return RESULT_FAILED, "Windows Update could not fetch this driver."
    if code == ORC_ABORTED:
        return RESULT_ABORTED, "The download was stopped before it finished."
    if code in (ORC_NOT_STARTED, ORC_IN_PROGRESS):
        return RESULT_FAILED, "The download never completed."
    return RESULT_FAILED, f"Windows Update returned an unknown result ({code})."


class _ComProgressSink:
    """Best-effort bridge from WUA's progress callback to ``emit``.

    WUA reports progress per update index; the caller maps indexes to
    devices, so the sink only needs the ordered titles. If the COM
    event plumbing can't be set up on a given machine, the download
    still works — only the mid-download progress notes go quiet.
    """

    def __init__(self, titles: list[str], emit):
        self._titles = titles
        self._emit = emit

    def OnProgressChanged(self, progress):  # COM callback name — WUA's API
        try:
            count = len(self._titles)
            for index in range(count):
                entry = progress.GetUpdateResult(index)
                percent = _com_field(entry, "PercentComplete", None)
                if percent is None:
                    continue
                if isinstance(percent, float) and not math.isfinite(percent):
                    continue
                self._emit(
                    ProgressEvent(
                        kind=EVENT_UPDATE_PROGRESS,
                        title=self._titles[index],
                        # A percentage means 0–100; anything outside
                        # that is WUA noise, clamped before a screen
                        # ever renders it.
                        percent=max(0, min(100, int(percent))),
                    )
                )
        except Exception:  # a progress hiccup must never sink a download
            log.debug("WUA progress callback failed", exc_info=True)


class ComUpdateDownloader:
    """The real Windows downloader, backed by the WUA COM session.

    Only ``Download`` is ever called on it — there is no installer
    object anywhere in this class, by design and by test.
    """

    def __init__(self, session=None):
        if session is None:
            import win32com.client  # pywin32, Windows-only by design

            session = win32com.client.Dispatch("Microsoft.Update.Session")
        self._session = session

    def download(
        self,
        updates: list[Any],
        emit: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadResult]:
        if not updates:
            # An empty batch is a valid answer, not a reason to make
            # Windows Update build machinery around nothing.
            return []

        # Phase A: per-update preparation. EULA acceptance happens
        # here, explicitly, and is remembered per update. A driver
        # whose terms can't be accepted fails alone — never silently,
        # never by sinking the batch — and it is NOT handed to the
        # downloader afterwards: Charon never asks Windows Update to
        # fetch an update whose required terms were not accepted,
        # and on a real PC one unaccepted update in the collection
        # can fail the whole download job for all the others.
        prepared: list[tuple[bool, int | None, str, str | None]] = []
        for update in updates:
            if update is None:
                prepared.append(
                    (
                        False,
                        None,
                        "Charon was handed an empty update entry, "
                        "so there was nothing to download for it.",
                        "empty update object",
                    )
                )
                continue
            title = str(_com_field(update, "Title", ""))
            size_bytes = _size_bytes(
                _com_field(update, "MaxDownloadSize", None)
            )
            if not bool(_com_field(update, "RequiresUserAcceptance", False)):
                prepared.append((False, size_bytes, "", None))
                continue
            try:
                update.AcceptEula()
            except Exception as exc:
                log.warning("EULA acceptance failed for %s: %s", title, exc)
                prepared.append(
                    (
                        False,
                        size_bytes,
                        "Charon could not accept the license terms "
                        "Windows Update requires for this driver.",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            log.info("License terms accepted for %s", title)
            prepared.append((True, size_bytes, "", None))

        failed_early = {
            i for i, p in enumerate(prepared) if p[3] is not None
        }
        if len(failed_early) == len(updates):
            # The batch never reached Windows Update at all.
            return [
                DownloadResult(
                    index=i,
                    status=RESULT_FAILED,
                    eula_accepted=p[0],
                    size_bytes=p[1],
                    detail=p[2],
                )
                for i, p in enumerate(prepared)
            ]

        # The live batch: only updates that survived preparation, in
        # input order. The downloader's result indexes address THIS
        # collection, so Phase C translates positions back to input
        # indexes — the per-update contract never shifts.
        live_indexes = [
            i for i in range(len(updates)) if i not in failed_early
        ]

        # Phase B: the batch fetch. A WUA-level exception here —
        # building the collection or downloader included — means the
        # download could not begin: a designed failure for the
        # caller, not a per-update result and never a raw COM error
        # on a user's screen. The progress sink is best-effort:
        # pywin32 exposes the sink object to COM directly; if a
        # machine refuses that plumbing, the download runs without
        # mid-flight progress notes.
        try:
            collection = self._session.CreateUpdateCollection()
            for i in live_indexes:
                collection.Add(updates[i])
            downloader = self._session.CreateUpdateDownloader()
            downloader.Updates = collection
        except Exception as exc:
            raise WuaDownloadUnavailable(
                "Windows Update couldn't start the download. Nothing "
                "was downloaded, and nothing was installed.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        sink = None
        if emit is not None:
            titles = [
                str(_com_field(updates[i], "Title", "")) for i in live_indexes
            ]
            sink = _ComProgressSink(titles, emit)
        try:
            if sink is not None:
                try:
                    downloader.Download(sink)
                except Exception:
                    log.debug(
                        "progress callback refused; downloading "
                        "without progress notes",
                        exc_info=True,
                    )
                    downloader.Download()
            else:
                downloader.Download()
        except Exception as exc:
            raise WuaDownloadUnavailable(
                "Windows Update couldn't start the download. Nothing "
                "was downloaded, and nothing was installed.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        # Phase C: per-update verdicts, read individually. One
        # driver's failure is its own result — it never repaints the
        # batch, and a success is never invented for it. A verdict
        # that can't even be read is that update's failure, not the
        # batch's crash.
        by_index: dict[int, DownloadResult] = {}
        for i, p in enumerate(prepared):
            if p[3] is not None:
                by_index[i] = DownloadResult(
                    index=i,
                    status=RESULT_FAILED,
                    eula_accepted=p[0],
                    size_bytes=p[1],
                    detail=p[2] or p[3],
                )
        for position, i in enumerate(live_indexes):
            update = updates[i]
            eula_accepted, size_bytes = prepared[i][0], prepared[i][1]
            try:
                entry = downloader.GetUpdateResult(position)
            except Exception as exc:
                log.warning(
                    "no verdict readable for update %s: %s",
                    _com_field(update, "Title", ""),
                    exc,
                )
                by_index[i] = DownloadResult(
                    index=i,
                    status=RESULT_FAILED,
                    eula_accepted=eula_accepted,
                    size_bytes=size_bytes,
                    detail="Windows Update gave no verdict for "
                    "this driver — Charon counts that as not "
                    "downloaded.",
                )
                continue
            code = _com_field(entry, "ResultCode", None)
            hresult = _com_field(entry, "HResult", None)
            if isinstance(code, bool) or not isinstance(code, int):
                by_index[i] = DownloadResult(
                    index=i,
                    status=RESULT_FAILED,
                    eula_accepted=eula_accepted,
                    size_bytes=size_bytes,
                    detail="Windows Update gave no verdict for "
                    "this driver — Charon counts that as not "
                    "downloaded.",
                )
                continue
            status, status_note = _status_for_code(code)
            detail = status_note
            if status == RESULT_SUCCEEDED and not bool(
                _com_field(update, "IsDownloaded", False)
            ):
                # Windows Update claimed success but staged nothing.
                # Truth beats the claim.
                status = RESULT_FAILED
                detail = (
                    "Windows Update reported success, but the driver "
                    "is not staged on this PC — Charon counts that "
                    "as not downloaded."
                )
            by_index[i] = DownloadResult(
                index=i,
                status=status,
                result_code=code,
                hresult=(
                    int(hresult)
                    if isinstance(hresult, int)
                    and not isinstance(hresult, bool)
                    else None
                ),
                eula_accepted=eula_accepted,
                size_bytes=size_bytes,
                detail=detail,
            )
        return [by_index[i] for i in range(len(updates))]


def download_updates(
    updates: list[Any],
    downloader: UpdateDownloader | None = None,
    emit: Callable[[ProgressEvent], None] | None = None,
) -> list[DownloadResult]:
    """Stage a batch of WUA updates (download only — never install).

    Returns one :class:`DownloadResult` per input update, in order.
    Raises :class:`WuaDownloadUnavailable` (plain-language) only when
    the download could not begin at all.
    """
    if downloader is None:
        if not sys.platform.startswith("win"):
            raise WuaDownloadUnavailable(
                "Driver downloads can only happen on a Windows PC.",
                detail=f"sys.platform={sys.platform}",
            )
        try:
            downloader = ComUpdateDownloader()
        except Exception as exc:
            raise WuaDownloadUnavailable(
                "Charon couldn't reach Windows Update on this PC.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
    return downloader.download(updates, emit=emit)
