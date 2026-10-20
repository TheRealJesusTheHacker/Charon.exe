"""THE INSTALL SEAM — one update at a time, honest verdicts.

Every test drives ``charon.wua_install`` through the fake COM
session: no Windows, no real installs. The load-bearing cases:

- one update per install call — never a bulk blast;
- verdicts map honestly (including success-claimed-but-not-installed);
- a restart requirement is reported, never acted on;
- machinery that can't be built is a designed refusal, while a
  failure mid-install is that update's own verdict.
"""

from __future__ import annotations

import pytest

from charon.wua_install import (
    RESULT_ABORTED,
    RESULT_FAILED,
    RESULT_SUCCEEDED,
    RESULT_SUCCEEDED_WITH_ERRORS,
    ComUpdateInstaller,
    WuaInstallUnavailable,
    install_update,
)
from conftest import FakeComSession, FakeComUpdate


def _update(title="Driver A", **kwargs):
    return FakeComUpdate(title=title, version="2.0", **kwargs)


def test_install_one_success_marks_update_installed():
    session = FakeComSession()
    installer = ComUpdateInstaller(session, collection_factory=session.make_collection)
    update = _update()
    outcome = installer.install_one(update)

    assert outcome.status == RESULT_SUCCEEDED
    assert outcome.result_code == 2
    assert outcome.reboot_required is False
    assert update.IsInstalled is True
    # Exactly one call, exactly one update in it.
    assert session.install_calls == [[update]]
    assert session.installer_constructed == 1


@pytest.mark.parametrize(
    "code,status",
    [
        (3, RESULT_SUCCEEDED_WITH_ERRORS),
        (4, RESULT_FAILED),
        (5, RESULT_ABORTED),
        (0, RESULT_FAILED),
        (1, RESULT_FAILED),
        (99, RESULT_FAILED),
    ],
)
def test_install_verdict_mapping(code, status):
    session = FakeComSession(install_codes_by_title={"Driver A": code})
    outcome = ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(_update())
    assert outcome.status == status
    assert outcome.result_code == code


def test_reboot_required_is_reported_not_acted_on():
    session = FakeComSession(reboot_titles={"Driver A"})
    outcome = ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(_update())
    assert outcome.status == RESULT_SUCCEEDED
    assert outcome.reboot_required is True
    # The seam has no restart member to call — the fake session
    # would have raised AssertionError on any unexpected access.


def test_success_claimed_but_not_installed_counts_as_failed():
    session = FakeComSession(claims_success_not_installed={"Driver A"})
    update = _update()
    outcome = ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(update)
    assert outcome.status == RESULT_FAILED
    assert "not installed" in outcome.detail
    assert update.IsInstalled is False


def test_mid_install_failure_is_the_updates_own_verdict():
    session = FakeComSession(install_raises_titles={"Driver A"})
    outcome = ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(_update())
    assert outcome.status == RESULT_FAILED
    assert "problem while installing" in outcome.detail


def test_machinery_failure_is_a_designed_refusal():
    session = FakeComSession(installer_setup_raises=True)
    with pytest.raises(WuaInstallUnavailable) as excinfo:
        ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(_update())
    assert "couldn't start the install" in excinfo.value.user_message
    assert excinfo.value.detail  # the technical half is logged, not shown raw


def test_empty_update_fails_alone_without_machinery():
    session = FakeComSession()
    outcome = ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(None)
    assert outcome.status == RESULT_FAILED
    assert session.installer_constructed == 0
    assert session.install_calls == []


def test_install_update_entry_point_uses_injected_installer():
    session = FakeComSession()
    outcome = install_update(
        _update(), installer=ComUpdateInstaller(session, collection_factory=session.make_collection)
    )
    assert outcome.status == RESULT_SUCCEEDED


def test_installer_builds_no_downloader():
    session = FakeComSession()
    ComUpdateInstaller(session, collection_factory=session.make_collection).install_one(_update())
    assert session.downloader_constructed == 0
    assert session.download_calls == []
