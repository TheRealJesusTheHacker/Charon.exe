# PyInstaller spec for CHARON.EXE — the portable edition.
#
# Build (from the repo root, on Windows):
#     pyinstaller packaging/Charon.spec --noconfirm
# Output: dist/Charon/  (a one-folder build: Charon.exe plus its
# runtime libraries in the same folder)
#
# One-FOLDER, not one-file, on purpose: this is the portable
# edition. The whole folder unzips anywhere — a USB stick for the
# Sentinel IT Toolkit included — and runs with no install and no
# Python on the machine. A one-folder build also starts faster and
# shows its contents honestly; nothing is unpacked to a temp
# folder at launch.
#
# No console window: the portable edition is the window. The
# launcher (packaging/launch_charon.py) opens the GUI when run
# with no arguments and routes any argument to the same CLI the
# source tree runs.
#
# PySide6 is bundled by PyInstaller's Qt hooks. The pywin32 COM
# stack is pulled in via hiddenimports because Charon's Windows
# Update Agent seams import it lazily, inside functions, by
# design (charon/wua.py, charon/wua_download.py,
# charon/wua_install.py) — the package imports cleanly on any
# platform, so static analysis never sees those imports on its
# own. The release workflow greps the PyInstaller warn file for
# exactly these names, so a build that silently dropped them
# fails there instead of on a user's machine.
#
# Charon ships no data files: the theme is code, the palette is
# code, and the test fixtures are not part of the product — so
# datas is empty and that is correct, not an omission.

from pathlib import Path

# PyInstaller 6 does NOT inject the Windows version-info classes
# into the spec namespace — they must be imported explicitly, or
# the build dies with NameError the first time the spec runs
# (the same lesson Argus recorded in its spec). Import exactly
# the classes used below.
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VSVersionInfo,
    VarFileInfo,
    VarStruct,
)

ROOT = Path(SPECPATH).parent

# Windows version resource for the exe: File Explorer, the task
# manager, and SmartScreen all read THIS — an exe with no version
# info looks anonymous. Version 1.0.6 matches charon.__version__
# (the release workflow's tag check keeps them from drifting).
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(1, 0, 5, 0),
        prodvers=(1, 0, 5, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct(
                            "CompanyName", "Sentinel Safeguard LLC"
                        ),
                        StringStruct(
                            "FileDescription",
                            "CHARON.EXE - Windows driver updater",
                        ),
                        StringStruct("FileVersion", "1.0.6.0"),
                        StringStruct("InternalName", "Charon"),
                        StringStruct(
                            "LegalCopyright",
                            "(c) Sentinel Safeguard LLC",
                        ),
                        StringStruct("OriginalFilename", "Charon.exe"),
                        StringStruct("ProductName", "CHARON.EXE"),
                        StringStruct("ProductVersion", "1.0.6"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "packaging" / "launch_charon.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Windows Update Agent COM stack (lazy imports in the
        # charon/wua*.py seams, Windows-only by design).
        "win32com",
        "win32com.client",
        "pythoncom",
        "pywintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Charon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Charon",
)
