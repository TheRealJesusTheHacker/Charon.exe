"""CHARON.EXE — Windows Update Agent seam (INSTALL ONLY).

This is the one module in the package that may ask Windows Update
to apply an update. It exists for Phase 4, the crossing itself, and
it is only ever reached through ``charon.apply``, which presents a
verified toll at the Phase 3 gate before a single install is
requested. Installing lives here and nowhere else — the search seam
asks, the download seam fetches, this seam applies, and the test
suite pins that separation.

How the crossing works, mechanically: Windows Update installs
through an installer object. Charon hands it **one update at a
time** — a collection holding exactly one update — so every verdict
read back belongs to exactly one driver, and the apply engine can
check the device and hold the line between crossings. There is no
bulk blast anywhere in this seam.

Honesty rules, mirroring the download seam's:

- A "success" Windows Update claims but the PC doesn't show (the
  update still reports not-installed afterwards) counts as failed.
  Truth beats the claim.
- A verdict that can't be read is that update's failure, never a
  crash and never an invented success.
- A restart Windows Update asks for is **reported**, never obeyed:
  this seam has no way to restart a PC and never will. Rebooting is
  the owner's act, on the owner's schedule.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .download import ProgressEvent

log = logging.getLogger("charon.wua_install")

# WUA OperationResultCode values — the same enum the download seam
# reads after a fetch; here they are the per-update verdicts of an
# install, read from the installation result for that one update.
ORC_NOT_STARTED = 0
ORC_IN_PROGRESS = 1
ORC_SUCCEEDED = 2
ORC_SUCCEEDED_WITH_ERRORS = 3
ORC_FAILED = 4
ORC_ABORTED = 5

# Charon's own result vocabulary for the crossing (also the apply
# ledger's).
RESULT_SUCCEEDED = "succeeded"
RESULT_SUCCEEDED_WITH_ERRORS = "succeeded_with_errors"
RESULT_FAILED = "failed"
RESULT_ABORTED = "aborted"


class WuaInstallUnavailable(Exception):
    """Windows Update could not be asked to install (designed failure).

    Plain-language message for the user; technical detail goes to
    the log. Distinct from a per-update failure: this means the
    install could not even begin.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class InstallOutcome:
    """The honest outcome of one update's crossing."""

    status: str  # one of the RESULT_* constants
    result_code: int | None = None  # raw WUA code, for the log/ledger
    hresult: int | None = None  # raw WUA error, when present
    reboot_required: bool = False  # reported, NEVER acted on here
    detail: str = ""


class UpdateInstaller(Protocol):
    """The seam: anything that can apply one WUA update.

    ``update`` is an opaque WUA update object exactly as a fresh
    search returned it (never reconstructed, never substituted).
    Implementations return the update's own verdict — one call, one
    update, one outcome.
    """

    def install_one(
        self,
        update: Any,
        emit: Callable[[ProgressEvent], None] | None = None,
    ) -> InstallOutcome:
        ...


def _com_field(obj, name: str, default=None):
    """Read one COM property defensively — any may be absent."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def update_requires_eula(update) -> bool:
    """Does this update require the user to accept license terms?

    Read here, in the seam, so the apply engine never reaches into
    COM properties itself. An unreadable answer counts as "no
    requirement stated" — the Phase 2 record is the authority on
    whether terms were accepted, and the install itself will fail
    honestly at Windows Update if terms are outstanding.
    """
    return bool(_com_field(update, "RequiresUserAcceptance", False))


def update_is_downloaded(update) -> bool:
    """Is this update's staged copy still in the Windows Update cache?"""
    return bool(_com_field(update, "IsDownloaded", False))


def _status_for_code(code: int) -> tuple[str, str]:
    """Map a WUA result code to Charon's status + plain-language note."""
    if code == ORC_SUCCEEDED:
        return RESULT_SUCCEEDED, ""
    if code == ORC_SUCCEEDED_WITH_ERRORS:
        return (
            RESULT_SUCCEEDED_WITH_ERRORS,
            "Windows Update installed this driver, but reported "
            "errors along the way — Charon will check the device "
            "before carrying on.",
        )
    if code == ORC_FAILED:
        return RESULT_FAILED, "Windows Update could not install this driver."
    if code == ORC_ABORTED:
        return RESULT_ABORTED, "The install was stopped before it finished."
    if code in (ORC_NOT_STARTED, ORC_IN_PROGRESS):
        return RESULT_FAILED, "The install never completed."
    return RESULT_FAILED, f"Windows Update returned an unknown result ({code})."


class ComUpdateInstaller:
    """The real Windows installer, backed by the WUA COM session.

    Only the installer machinery is ever built here, one update per
    call — and nothing in this class can download, search, create
    restore points, or restart the PC.
    """

    def __init__(self, session=None):
        if session is None:
            import win32com.client  # pywin32, Windows-only by design

            session = win32com.client.Dispatch("Microsoft.Update.Session")
        self._session = session

    def install_one(
        self,
        update: Any,
        emit: Callable[[ProgressEvent], None] | None = None,
    ) -> InstallOutcome:
        if update is None:
            return InstallOutcome(
                status=RESULT_FAILED,
                detail="Charon was handed an empty update entry, so "
                "there was nothing to install for it.",
            )
        title = str(_com_field(update, "Title", ""))

        # Building the machinery is part of beginning: a failure
        # here means the install could not begin at all — a
        # designed refusal for the caller, never a raw COM error on
        # a user's screen and never a per-update verdict for an
        # install that was never attempted.
        try:
            collection = self._session.CreateUpdateCollection()
            collection.Add(update)
            installer = self._session.CreateUpdateInstaller()
            installer.Updates = collection
        except Exception as exc:
            raise WuaInstallUnavailable(
                "Windows Update couldn't start the install. Nothing "
                "was installed.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        # The crossing itself. A failure mid-install is this
        # update's own verdict — Windows Update may have partially
        # acted, so the honest word is "failed", with the device
        # check downstream deciding whether the ferry holds.
        try:
            result = installer.Install()
        except Exception as exc:
            log.warning("install failed for %s: %s", title, exc)
            return InstallOutcome(
                status=RESULT_FAILED,
                detail="Windows Update hit a problem while "
                "installing this driver.",
            )

        reboot_required = bool(_com_field(result, "RebootRequired", False))

        # The per-update verdict, read individually. A verdict that
        # can't even be read is this update's failure — never the
        # batch's crash, never an invented success.
        try:
            entry = result.GetUpdateResult(0)
        except Exception as exc:
            log.warning("no verdict readable for %s: %s", title, exc)
            return InstallOutcome(
                status=RESULT_FAILED,
                reboot_required=reboot_required,
                detail="Windows Update gave no verdict for this "
                "driver — Charon counts that as not installed.",
            )
        code = _com_field(entry, "ResultCode", None)
        hresult = _com_field(entry, "HResult", None)
        if isinstance(code, bool) or not isinstance(code, int):
            return InstallOutcome(
                status=RESULT_FAILED,
                reboot_required=reboot_required,
                detail="Windows Update gave no verdict for this "
                "driver — Charon counts that as not installed.",
            )
        status, status_note = _status_for_code(code)
        detail = status_note
        if status == RESULT_SUCCEEDED and not bool(
            _com_field(update, "IsInstalled", False)
        ):
            # Windows Update claimed the crossing but the driver is
            # not on the PC. Truth beats the claim.
            status = RESULT_FAILED
            detail = (
                "Windows Update reported success, but the driver "
                "is not installed on this PC — Charon counts that "
                "as not crossed."
            )
        return InstallOutcome(
            status=status,
            result_code=code,
            hresult=(
                int(hresult)
                if isinstance(hresult, int)
                and not isinstance(hresult, bool)
                else None
            ),
            reboot_required=reboot_required,
            detail=detail,
        )


def install_update(
    update: Any,
    installer: UpdateInstaller | None = None,
    emit: Callable[[ProgressEvent], None] | None = None,
) -> InstallOutcome:
    """Apply one WUA update (install only — the crossing itself).

    Raises :class:`WuaInstallUnavailable` (plain-language) only when
    the install could not begin at all; every outcome of an attempted
    install comes back as an :class:`InstallOutcome`.
    """
    if installer is None:
        if not sys.platform.startswith("win"):
            raise WuaInstallUnavailable(
                "Driver installs can only happen on a Windows PC.",
                detail=f"sys.platform={sys.platform}",
            )
        try:
            installer = ComUpdateInstaller()
        except Exception as exc:
            raise WuaInstallUnavailable(
                "Charon couldn't reach Windows Update on this PC.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
    return installer.install_one(update, emit=emit)
