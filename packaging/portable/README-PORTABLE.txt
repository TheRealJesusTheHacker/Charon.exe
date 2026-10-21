CHARON.EXE - Portable Edition
Sentinel Safeguard LLC

WHAT THIS IS
------------
Charon, Sentinel's Windows driver updater, in portable form:
the full window and the whole Crossing - Scan, Download, the
Toll (a restore point, created and verified), then Apply -
in a folder that runs with no install and no Python on the
machine.

HOW TO RUN IT
-------------
Unzip CharonPortable.zip anywhere - a folder on the PC, or a
USB stick such as the Sentinel IT Toolkit. Open the Charon
folder and double-click Charon.exe. That is all: nothing is
installed, nothing is registered, and deleting the folder
removes the program.

Run as administrator when the crossing reaches the toll or the
apply step - Windows requires it to create a restore point or
install a driver. Scanning is not administrator work. (Right-
click Charon.exe and choose "Run as administrator" for a full
crossing.)

WHAT'S DIFFERENT FROM RUNNING FROM SOURCE (honest list)
-------------------------------------------------------
- Nothing about the Crossing is different. The order is fixed -
  Scan, Download, Toll, Apply - the toll is created AND verified
  for this run before any driver is applied, and a toll that
  does not verify stops the crossing. Portable changes the
  packaging, never the rules.
- Charon's download ledger still lives in the standard app-
  state location for the machine, %PROGRAMDATA%\Charon\
  ledger.json - the ledger is the machine's record of its
  crossings, and restore points live in Windows System Restore
  on the machine, so the record stays with the machine it
  describes even when Charon itself arrives on a USB stick.
- Downloaded drivers are staged by the Windows Update channel
  into the Windows Update cache on the machine, exactly as
  from source. Charon never downloads drivers from third-party
  driver sites.
- The command line is still there: run Charon.exe with a verb
  (scan, download, toll, apply, manifest, gui) from a terminal in this
  folder and it behaves as it does from source.

Charon never crosses without the toll. Nobody crosses without
paying it - and the toll is a restore point, verified, every
time.
