"""SAFETY INVARIANTS — capability is confined to its phase's seam.

Phase 1 was read-only, and the original file pinned exactly that:
no downloads, no installs, no restore points anywhere in the
package. Each later phase arrives as a deliberate rewrite of this
file, in the open — never as a quiet edit elsewhere:

- **Phase 2** confined the WUA downloader surface
  (``CreateUpdateDownloader`` and friends) to ``wua_download.py``,
  the one seam that may fetch an update. Installing stayed
  forbidden everywhere.
- **Phase 3** confined restore points to ``toll.py``, the one seam
  that may create or query them — and Charon may still never flip
  System Protection itself (``Enable-/Disable-ComputerRestore``
  forbidden everywhere).

What never changes: driver mutation and update installation have
NO home in this package until their own phase arrives, and each
phase's capability may exist in exactly one seam module.
"""

from pathlib import Path

import pytest

from charon.platform_inventory import CIM_QUERY_SCRIPT
from conftest import RecordingRunner

PACKAGE = Path(__file__).resolve().parent.parent / "charon"

# The complete module census. A new module means a deliberate edit
# here, in review, not a surprise.
EXPECTED_MODULES = {
    "__init__.py",
    "__main__.py",
    "cli.py",
    "download.py",
    "inventory.py",
    "matching.py",
    "model.py",
    "platform_inventory.py",
    "toll.py",
    "wua.py",
    "wua_download.py",
}

# Capability that has no home ANYWHERE in the package yet.
FORBIDDEN_TOKENS = [
    # Driver mutation via pnputil or anything else.
    "add-driver",
    "delete-driver",
    "remove-driver",
    "/install",
    # WUA machinery that APPLIES updates — the crossing is Phase 4,
    # behind the toll, and not one line early.
    "CreateUpdateInstaller",
    ".Install(",
    "BeginInstall",
    # Charon never flips System Protection itself — detecting the
    # state and guiding the owner is the whole of its mandate.
    "Enable-ComputerRestore",
    "Disable-ComputerRestore",
    "vssadmin",
    # Shelling out loosely / downloading from the network.
    "os.system",
    "shell=True",
    "urlopen",
    "Invoke-WebRequest",
    "Start-BitsTransfer",
]

# Capability confined to exactly one seam module.
CONFINED_TOKENS = {
    # Phase 2: the fetch surface lives in the download seam only.
    "wua_download.py": [
        "CreateUpdateDownloader",
        "CreateUpdateCollection",
        ".Download(",
        "BeginDownload",
        "AcceptEula",
    ],
    # Phase 3: restore points live in the toll seam only.
    "toll.py": [
        "Checkpoint-Computer",
        "Get-ComputerRestorePoint",
        "CreateRestorePoint",
        "SRSetRestorePoint",
        "SystemRestore",
        "RestorePoint",
    ],
}

# Modules allowed to execute operating-system commands: the seams.
COMMAND_SEAMS = {"platform_inventory.py", "toll.py"}


def _sources() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8") for p in PACKAGE.glob("*.py")
    }


def test_module_census():
    assert set(_sources()) == EXPECTED_MODULES


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_no_forbidden_capability_anywhere(token):
    for name, text in _sources().items():
        assert token not in text, f"{token!r} found in charon/{name}"


@pytest.mark.parametrize(
    "token,home",
    [
        (token, home)
        for home, tokens in CONFINED_TOKENS.items()
        for token in tokens
    ],
)
def test_confined_capability_lives_only_in_its_seam(token, home):
    for name, text in _sources().items():
        if name == home:
            continue
        assert token not in text, (
            f"{token!r} belongs to charon/{home} but was found in "
            f"charon/{name}"
        )


def test_subprocess_confined_to_seams():
    # Only the seam modules may execute commands. Other modules may
    # NAME the data sources (identifiers like ``pnputil_entries``,
    # doc references to date formats) — what they may never contain
    # is a command line: no subprocess use, and none of the quoted
    # argv literals that would build one.
    command_literals = ('"pnputil"', '"powershell"', '"/enum-drivers"', '"-Command"')
    for name, text in _sources().items():
        if name in COMMAND_SEAMS:
            continue
        assert "subprocess" not in text, f"subprocess leaked into {name}"
        for literal in command_literals:
            assert literal not in text, (
                f"command literal {literal} leaked into {name}"
            )


def test_wua_session_confined_to_wua_modules():
    # Both WUA seams hold a session of their own (the searcher asks,
    # the downloader fetches); the searcher factory itself belongs
    # to the search seam alone.
    for name, text in _sources().items():
        if name in ("wua.py", "wua_download.py"):
            continue
        assert "Microsoft.Update" not in text, f"WUA leaked into {name}"
    for name, text in _sources().items():
        if name == "wua.py":
            continue
        assert "CreateUpdateSearcher" not in text


def test_cim_script_has_no_mutating_cmdlets():
    for verb in (
        "Set-",
        "New-",
        "Remove-",
        "Add-",
        "Start-",
        "Stop-",
        "Invoke-",
        "Enable-",
        "Disable-",
    ):
        assert verb not in CIM_QUERY_SCRIPT, f"{verb}* in the CIM query"


def test_wua_module_searches_and_only_searches():
    text = _sources()["wua.py"]
    assert ".Search(" in text
    assert 'Dispatch("Microsoft.Update.Session")' in text
    # The search seam never grew a fetch or an apply while nobody
    # was watching: those surfaces live in their own seams.
    assert "CreateUpdateDownloader" not in text
    assert "CreateUpdateInstaller" not in text


def test_toll_scripts_are_the_pinned_ones():
    from charon.toll import (
        RP_CREATE_SCRIPT_TEMPLATE,
        RP_LIST_SCRIPT,
        TOLL_PROBE_SCRIPT,
    )

    # Creation runs the one pinned cmdlet with our description;
    # enumeration is the one pinned query; the probe is read-only.
    assert "Checkpoint-Computer" in RP_CREATE_SCRIPT_TEMPLATE
    assert "{description}" in RP_CREATE_SCRIPT_TEMPLATE
    assert "Get-ComputerRestorePoint" in RP_LIST_SCRIPT
    assert "Checkpoint-Computer" not in RP_LIST_SCRIPT
    assert "Checkpoint-Computer" not in TOLL_PROBE_SCRIPT
    # The probe reads protection state from shadow storage; it does
    # not create, enable, or disable anything.
    assert "Win32_ShadowStorage" in TOLL_PROBE_SCRIPT
    for script in (TOLL_PROBE_SCRIPT, RP_LIST_SCRIPT):
        for verb in ("Set-", "Remove-", "Add-", "Enable-", "Disable-"):
            assert verb not in script, f"{verb}* in a toll query script"


def test_behavioral_commands_are_exactly_the_pinned_ones(
    cim_text, pnputil_text
):
    """Driving the real collection path issues two read-only commands."""
    from charon.platform_inventory import (
        PNPUTIL_ENUM_ARGS,
        POWERSHELL_ARGS,
        collect_inventory,
    )

    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    collect_inventory(runner=runner)
    assert runner.calls == [list(POWERSHELL_ARGS), list(PNPUTIL_ENUM_ARGS)]


def test_behavioral_download_run_issues_exactly_the_pinned_sequence(
    monkeypatch, tmp_path, cim_text, pnputil_text, wua_updates
):
    """Phase 2's behavioral pin: a full staged download issues the
    scan's two pinned read-only commands, two searches under the one
    pinned criteria (the scan's, then the revalidation's — the second
    keeping the seam objects), exactly one downloader construction
    and one batch download — and the fakes raise on ANY other
    capability, so an install or restore call anywhere in the run
    would detonate this test."""
    from charon import cli
    from charon.platform_inventory import (
        PNPUTIL_ENUM_ARGS,
        POWERSHELL_ARGS,
        collect_inventory,
    )
    from charon.wua import search_driver_updates
    from charon.wua_download import ComUpdateDownloader, download_updates
    from conftest import FakeComSession, FakeComUpdate, FakeSearcher

    com_updates = [
        FakeComUpdate(
            title=u["title"],
            driver_model=u["driver_model"],
            driver_class=u["driver_class"],
            provider=u["provider"],
            version=u["version"],
            driver_date=u["driver_date"],
            hardware_ids=u["hardware_ids"],
        )
        for u in wua_updates
    ]
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=[c.raw() for c in com_updates])
    session = FakeComSession()
    fake_runner = runner  # aliased: the lambda's own arg would shadow it

    monkeypatch.setattr(
        cli,
        "collect_inventory",
        lambda runner=None: collect_inventory(runner=fake_runner),
    )
    monkeypatch.setattr(
        cli,
        "search_driver_updates",
        lambda searcher_arg=None, keep_objects=False: search_driver_updates(
            searcher, keep_objects=keep_objects
        ),
    )
    monkeypatch.setattr(
        cli,
        "download_updates",
        lambda objects, downloader=None, emit=None: download_updates(
            objects, downloader=ComUpdateDownloader(session), emit=emit
        ),
    )

    report = cli.run_scan()
    results, _ledger = cli.run_download(
        report, ledger_path=tmp_path / "ledger.json"
    )

    assert runner.calls == [list(POWERSHELL_ARGS), list(PNPUTIL_ENUM_ARGS)]
    assert searcher.criteria_seen == [
        "IsInstalled=0 and Type='Driver'",
        "IsInstalled=0 and Type='Driver'",
    ]
    assert searcher.keep_objects_seen == [False, True]
    assert session.downloader_constructed == 1
    assert len(session.download_calls) == 1
    assert len(session.download_calls[0]) == 4
    assert all(r.status == "succeeded" for r in results)
