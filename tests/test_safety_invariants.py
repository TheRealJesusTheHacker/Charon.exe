"""SAFETY INVARIANTS — Phase 1 is read-only, and these tests pin it.

The package must contain NO code path that downloads, installs,
removes, or modifies a driver, and none that creates restore points
(the toll belongs to Phase 3). If a future change needs one of these
strings, it must arrive as a deliberate phase change that rewrites
this file in the open — never as a quiet edit elsewhere.
"""

from pathlib import Path

import pytest

from charon.platform_inventory import CIM_QUERY_SCRIPT
from conftest import RecordingRunner

PACKAGE = Path(__file__).resolve().parent.parent / "charon"

# The complete Phase 1 module census. A new module means a deliberate
# edit here, in review, not a surprise.
EXPECTED_MODULES = {
    "__init__.py",
    "__main__.py",
    "cli.py",
    "inventory.py",
    "matching.py",
    "model.py",
    "platform_inventory.py",
    "wua.py",
}

FORBIDDEN_TOKENS = [
    # Driver mutation via pnputil or anything else.
    "add-driver",
    "delete-driver",
    "remove-driver",
    "/install",
    # WUA machinery that fetches or applies updates.
    "CreateUpdateDownloader",
    "CreateUpdateInstaller",
    ".Download(",
    ".Install(",
    "BeginDownload",
    "BeginInstall",
    # Restore points are Phase 3's toll — not yet, not early.
    "Checkpoint-Computer",
    "SRSetRestorePoint",
    "RestorePoint",
    # Shelling out loosely / downloading from the network.
    "os.system",
    "shell=True",
    "urlopen",
    "Invoke-WebRequest",
    "Start-BitsTransfer",
]


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


def test_subprocess_confined_to_platform_seam():
    # Only the platform seam may execute commands. Other modules may
    # NAME the data sources (identifiers like ``pnputil_entries``,
    # doc references to date formats) — what they may never contain
    # is a command line: no subprocess use, and none of the quoted
    # argv literals that would build one.
    command_literals = ('"pnputil"', '"powershell"', '"/enum-drivers"', '"-Command"')
    for name, text in _sources().items():
        if name == "platform_inventory.py":
            continue
        assert "subprocess" not in text, f"subprocess leaked into {name}"
        for literal in command_literals:
            assert literal not in text, (
                f"command literal {literal} leaked into {name}"
            )


def test_wua_session_confined_to_wua_module():
    for name, text in _sources().items():
        if name == "wua.py":
            continue
        assert "Microsoft.Update" not in text, f"WUA leaked into {name}"
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
