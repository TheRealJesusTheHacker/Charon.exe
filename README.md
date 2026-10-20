# CHARON.EXE

**Sentinel Safeguard LLC — the Windows driver updater that pays the toll first.**

[![Version](https://img.shields.io/badge/version-v1.0.2-2ee06f?style=flat-square)](https://github.com/TheRealJesusTheHacker/Charon.exe/releases/tag/v1.0.2)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white)](https://github.com/TheRealJesusTheHacker/Charon.exe)
[![Python](https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![UI](https://img.shields.io/badge/UI-PySide6-41cd52?style=flat-square)](https://doc.qt.io/qtforpython-6/)
[![License](https://img.shields.io/badge/license-Proprietary-b03a2e?style=flat-square)](https://github.com/TheRealJesusTheHacker/Charon.exe/blob/main/LICENSE)

*Every driver ferried safely to the other side.*

---

## Overview

In the old stories, Charon ferried souls across the river Styx — and
nobody crossed without paying his toll. One coin, paid up front, no
exceptions.

CHARON.EXE is Sentinel Safeguard's ferryman for Windows drivers. It
carries every driver on a PC from the old version to the new one —
carefully, one crossing at a time — and the ancient rule still holds:

> **Nobody crosses without paying the toll.**
> **The toll is a restore point** — created and verified *before* a
> single update is applied. Every time. No exceptions.

If the toll can't be paid, the ferry doesn't move. That's not a
setting. That's the law of the river.

Charon is built for the people driver updates usually hurt: PC owners
and small-business clients who are not driver experts and cannot
afford to be. A bad network, display, or storage driver can leave a
machine unbootable or offline, and the people it happens to are
usually the ones least able to recover from it. Charon's whole design
answers that: know what's installed, fetch only legitimate updates
through the same Windows Update channel Microsoft itself uses, **pay
the toll first**, cross one driver at a time, check the device after
each crossing, and always keep the way back open — in plain language,
on designed screens, never as a raw error code.

Charon joins Sentinel Safeguard's medic-kit line:

- **Argus.exe** — antivirus & malware detection ("all eyes, all detection")
- **NitroCore** — system performance
- **VectorZip** — file compression
- **Charon.exe** — driver updates, with the toll paid first

## The Crossing

Every update run follows the same four steps, in this order, always.
The workflow is fixed in the code, not suggested by it:

| Step | What happens | What it can never do |
|---|---|---|
| 1. **Scan** | Inventory every installed driver; ask Windows Update what it offers; flag the outdated ones | Change anything — the scan is read-only |
| 2. **Download** | Stage the offered updates into the Windows Update cache and record them in Charon's ledger | Install anything, or silently substitute a different update |
| 3. **The Toll** | Create a System Restore point for this run and verify — by enumeration — that it actually exists | Be skipped, faked, or taken on trust |
| 4. **Apply** | Re-verify the toll at the gate, then install the staged drivers one at a time, checking each device after its crossing | Bulk-blast drivers, force a restart, or cross without the gate |

## Features — the five phases, all built

### Phase 1 — Driver inventory & scan engine

- Enumerates the installed drivers two ways and cross-checks them:
  a CIM query (`Win32_PnPSignedDriver`, via PowerShell, returned as
  JSON) plus `pnputil /enum-drivers` as cross-check and fallback.
- Per driver, the report — the **passenger manifest** — carries the
  device, provider, version, driver date, class, and signer status.
- Both sources are normalized into one manifest, and each device is
  matched to the updates Windows Update offers for it, in this order
  of trust:
  1. **Hardware ID** — the update is tied to the exact device.
  2. **Device model name** — allowed, but labeled lower confidence.
  3. **Date only** — version comparison outranks dates; a difference
     visible only in dates is reported at low confidence, because
     driver dates lie.
- Every device is classified honestly: `update_available`,
  `up_to_date`, or `unknown` — when Windows Update reports nothing
  for a device, Charon says so instead of guessing.
- The scan is read-only by design, and that is test-pinned: the suite
  asserts the exact OS commands issued.

### Phase 2 — Update sourcing & download staging

- Updates come from **one channel only: the Windows Update Agent
  API** — the legitimate route by which Microsoft itself offers
  driver updates for the installed hardware. Charon never scrapes
  third-party "driver download" sites: wrong-driver and malware
  risk, and beneath the product's standard.
- Staging truth, stated plainly: the Windows Update downloader
  stages into the **Windows Update cache**. The channel offers no
  Charon-owned target folder, and Charon does not pretend
  otherwise — Charon's own **download ledger** is the record of
  what is staged (per update: title, version, device, result,
  license-terms acceptance, size, timestamp), written atomically.
- Every download plan is **revalidated against a fresh Windows
  Update search** run moments before the fetch. An update that no
  longer resolves is marked `no_longer_available` and *nothing is
  downloaded in its place*.
- Every per-update verdict is read individually (succeeded /
  succeeded-with-errors / failed / aborted). A "success" Windows
  Update claims but didn't stage counts as failed. An update whose
  license terms require acceptance gets them accepted explicitly,
  and the acceptance is recorded per update.
- A subset can be staged (`--only`, repeatable), which is what the
  window's driver picker uses.

### Phase 3 — The toll (restore-point integration)

- The toll seam probes the PC first: is the caller an administrator,
  and is System Protection on for the system drive?
- **System Protection off is a designed refusal**, in plain
  language, with the how-to for turning it on. Charon never flips
  that setting itself, and it does not cross without the toll.
  Not-an-administrator is likewise a plain-language refusal.
- Creation is `Checkpoint-Computer`, named for the run. But
  *"creation requested" is not "toll paid"* — see
  [The toll, in detail](#the-toll-in-detail) below.
- The verified toll is recorded in the same ledger the downloads
  keep, so the record of a run is in one place.

### Phase 4 — Apply engine (the crossing itself)

- The staged set comes **from the ledger** — exactly what Phase 2
  staged, minus crossings an earlier run already applied. There is
  no other source of candidates.
- Administrator rights are confirmed, then **every staged update is
  re-validated against a fresh search**: a withdrawn or swapped
  update is `no_longer_available`, never substituted.
- Only then — moments before the first install —
  `require_paid_toll()` **re-verifies the toll against the live PC**
  (see the gate, below). The check and the crossing are one breath
  apart; no cached "paid" flag is ever trusted.
- Installs run through the Windows Update installer, **one update
  per call**. After each crossing the device is checked: still
  reporting in, carrying the new version, no longer offered as
  not-installed. A failed install or an unhealthy device **stops
  the line** — the remaining drivers are recorded as held back, not
  attempted.
- An update whose license terms were not recorded as accepted in
  Phase 2 blocks its driver alone (`eula_not_accepted`).
- A restart a driver needs is **reported, never forced** — no code
  path in the package can restart a PC.
- Every verdict lands in the ledger's `apply_runs`, and whenever a
  crossing fails, the summary names the toll's restore point as the
  way back.

### Phase 5 — The animated HUD window (v1.0.0)

- A single PySide6 window drives **the same composed pipeline as
  the CLI** — the interface owns no capability of its own (no COM,
  no commands), pinned by the safety-invariant suite.
- The journey is animated from the engine's own progress events, on
  a worker thread, so the screen only ever shows work actually
  happening: an animated scan sweep over the driver manifest, the
  river-crossing journey visual through Download → Toll → Apply,
  and animated per-driver progress.
- **Apply is locked until this visit's toll verifies** and the toll
  ticket stamps **TOLL PAID**. That lock is the real gate, not a
  visual trick: the engine still re-verifies the gate behind the
  button, and a gate that closes mid-flow returns the owner to the
  toll screen with the reason.
- Every state is a designed screen: scanning, updates found / none
  found, downloading, toll paid, toll refused (with the
  plain-language way forward), crossing, a driver held back, a
  restart needed (reported, never forced), and done.
- Sentinel green-and-black throughout: no light backgrounds, every
  text pairing pinned at ≥ 4.5:1 contrast by test, and no purple
  anywhere in the palette — also pinned by test.

## Installation

**The portable edition has shipped — in
[v1.0.2](https://github.com/TheRealJesusTheHacker/Charon.exe/releases/tag/v1.0.2).**
Download `CharonPortable.zip` from that release (its
`SHA256SUMS.txt` is attached alongside it), unzip it anywhere —
a folder on the PC or the Sentinel IT Toolkit USB — open the
`Charon` folder inside, and double-click `Charon.exe`. No
install, and no Python on the machine. The portable build is
wired and committed in this repository, under `packaging/`: a
PyInstaller one-folder spec, the pinned CI dependency list, the
portable readme that ships inside the zip, and the release
workflow (`.github/workflows/build-release.yml`) that — on
every published release — runs the full test suite, builds the
folder, smoke-tests the packaged window, and attaches
`CharonPortable.zip` and `SHA256SUMS.txt` to the release.
(A single self-installing `Charon.exe` stays on the roadmap's
backlog; see [Status & roadmap](#status--roadmap).)

Running from source — the other way to run Charon. What you need:

- A **Windows PC** to do real driver work. (Charon's logic and its
  test suite run anywhere; the inventory, Windows Update, and
  restore-point seams only operate on Windows, and say so in plain
  language anywhere else.)
- **Python 3**.
- **PySide6** — only for the window (`python -m charon gui`); the
  CLI is deliberately Qt-free.
- **pywin32** — on Windows, the Windows Update Agent seams reach
  the `Microsoft.Update.Session` COM object through it.
- **pytest** — only to run the test suite.

This repository ships no end-user `requirements.txt`, so the
source steps are exactly: (The pinned file under `packaging/`
is for the release pipeline's builds, not for source installs.)

```bat
git clone https://github.com/TheRealJesusTheHacker/Charon.exe.git
cd Charon.exe
pip install PySide6
pip install pywin32
python -m charon --version
```

Administrator rights are needed when Charon creates a restore point
or installs drivers; scanning is not administrator work.

## Usage

### The window — `python -m charon gui`

One window, one journey:

1. **Welcome.** The ferry at the dock, and one button: *Scan for
   driver updates*.
2. **Scanning.** The animated sweep runs while Charon inventories
   every driver and asks Windows Update what it offers. Nothing
   changes during a scan.
3. **Your drivers.** The manifest, one row per driver, each with
   its status. Drivers with updates are checked for download;
   uncheck any driver to leave it behind, then *Download selected
   drivers*. If everything is up to date, the window says exactly
   that — there's nothing for the ferry to carry today.
4. **Downloading.** Per-driver progress, animated from the real
   download events. Nothing is installed in this step.
5. **The toll.** *Pay the toll* creates this visit's restore point
   and verifies it. On success the ticket stamps **TOLL PAID**,
   showing the restore point's number and time — and only then does
   *Begin the crossing* unlock. On refusal, the window shows the
   plain-language reason (for example, System Protection is off,
   with the way to turn it on) and the crossing stays locked.
6. **The crossing.** Drivers install one at a time, each row
   following its own crossing. A driver that fails holds the rest
   of the line, and the screen says so. A driver that needs a
   restart reports it — the window never restarts the PC for you.
7. **Done.** A designed summary screen for the run, including the
   restore point to return to if anything feels wrong afterwards.

### The command line

The CLI runs the same pipeline, one verb per step of the Crossing:

```
python -m charon scan              # inventory drivers, flag outdated ones
python -m charon scan --json PATH  # also write the full report as JSON
python -m charon download          # stage outdated drivers (installs nothing)
python -m charon toll              # pay the toll: create + verify a restore point
python -m charon apply             # the crossing: install the staged drivers
python -m charon gui               # the window: the whole crossing, guided
python -m charon --version         # 1.0.2
```

| Command | Options | Notes |
|---|---|---|
| `scan` | `--json PATH` | Read-only. Prints the manifest table; `--json` also exports the full report for `download --from-report` |
| `download` | `--from-report PATH`, `--only DEVICE` (repeatable; by device ID or name), `--ledger PATH` | Fresh scan in-session unless `--from-report` is given; the plan is still re-checked against a fresh search either way |
| `toll` | `--ledger PATH` | Creates and verifies the restore point standalone and prints the receipt; applies nothing |
| `apply` | `--ledger PATH` | Takes exactly what `download` staged, re-validates, presents the toll at the gate, then crosses one driver at a time |
| `gui` | — | Opens the window described above |

Global flags come before the command: `--version`, and `--debug`
for technical detail in the log output. Exit codes are designed,
like everything else user-facing: `0` for a completed run, `2` for a
designed refusal or failure state (toll unpaid, a download that
couldn't begin, an unavailable seam) — explained in plain language
on screen, never as a traceback. Technical detail goes to the log.

A typical end-to-end run:

```bat
python -m charon scan --json scan.json
python -m charon download --from-report scan.json
python -m charon toll
python -m charon apply
```

Download stages updates into the Windows Update cache and applies
nothing. The toll creates a System Restore point and verifies it
exists. Apply takes exactly what download staged, re-checks each
update against a fresh Windows Update search, presents the toll at
the gate — re-verified against the PC at that moment — and installs
one driver at a time, checking the device after each.

## How it works

Charon is built as **pure logic around confined Windows seams**.
Every capability that touches the PC lives in exactly one module;
everything else is testable logic that receives that capability as
an injected callable. The safety-invariant suite scans the package
and proves the confinement holds.

| Module | Role |
|---|---|
| `charon/platform_inventory.py` | The only inventory seam: the read-only CIM device query plus `pnputil /enum-drivers` as cross-check/fallback |
| `charon/wua.py` | Search-only Windows Update Agent seam — asks what Windows Update offers. Exposes no download or install capability at all |
| `charon/wua_download.py` | The one download seam: the WUA downloader, per-update verdicts, license-terms acceptance |
| `charon/toll.py` | The one restore-point seam — probe, `Checkpoint-Computer` creation, enumeration — and the gate, `require_paid_toll()` |
| `charon/wua_install.py` | The one install seam: the Windows Update installer, one update per call, restarts reported and never acted on |
| `charon/inventory.py` / `charon/matching.py` | Pure logic: normalize both inventory sources into one manifest; match devices to offered updates (hardware ID > model > date-only) and classify honestly |
| `charon/download.py` | Pure logic: the download plan, fresh-search revalidation, progress events, and the ledger |
| `charon/apply.py` | Pure logic: the crossing — ledger staged set, admin check, fresh revalidation, the fresh gate, one-at-a-time installs, device checks, apply records |
| `charon/model.py` / `charon/cli.py` | The report model and the command line (table or JSON), with designed plain-language failure states |
| `charon/gui.py` / `charon/hud.py` / `charon/theme.py` | The window, the animated HUD widgets (scan sweep, journey, toll ticket, driver rows), and the Sentinel palette/stylesheet — the window owns no capability of its own |

### The ledger

Charon's download ledger is the run's single record: what download
staged, the verified toll, and every apply verdict (`apply_runs`).
It is written atomically through a locked writer, per update, never
as a blanket claim. By default it lives at
`%PROGRAMDATA%\Charon\ledger.json` on Windows (the XDG data
directory elsewhere); `--ledger PATH` overrides it for the
`download`, `toll`, and `apply` commands. The ledger is a record,
not a setting — losing it loses history, never safety: the gate
re-verifies against the PC itself, not against the ledger's word.

## The toll, in detail

The toll is the product's safety law, and verification is two-legged
because each leg alone can be fooled:

1. **This run's marker.** Every toll carries a unique marker (the
   run's timestamp plus a token). A point pays only if its
   description carries *this* run's marker — yesterday's restore
   point can never be passed off as today's.
2. **This run's timestamp.** The point must be stamped no earlier
   than this run's creation call (with 2 seconds of clock
   tolerance, because a point is stamped while the call runs).

That design exists for a real trap: **Windows silently skips a
requested restore point when a recent one already exists** (its
frequency limit). A skipped point carries neither this run's marker
nor its stamp, so it reads exactly as what it is — **unpaid** — and
the ferry does not move.

`require_paid_toll()` is the gate, and it is re-run fresh inside the
apply, moments before the first install: the candidate record (this
session's, else the ledger's) is re-verified against the PC's actual
restore points. A point deleted since it was paid closes the gate
again. Every toll failure mode — protection off, not an
administrator, skipped, undatable, unreadable, unrecordable —
resolves to one state: `TollUnpaid`. Charon fails toward not moving.

## Safety model

- **The toll law outranks every convenience.** No restore point, no
  update. There is no flag, setting, or override that crosses
  without it.
- **Fail-closed, by design.** Designed refusals are product states,
  not error handling: protection-off (with guidance), administrator
  required, an unverifiable toll, an update that vanished
  (`no_longer_available`, never substituted), unaccepted license
  terms (`eula_not_accepted`, that driver alone), a failed crossing
  (the line stops; the rest are held back).
- **Claims are verified, never trusted.** A download or install
  Windows Update claims succeeded but the PC doesn't show counts
  as failed — in download, in install, and at the gate.
- **One channel, one seam per capability.** Updates only ever come
  through the Windows Update Agent; downloads only in
  `wua_download.py`, restore points only in `toll.py`, installs
  only in `wua_install.py`, and restart machinery is forbidden
  package-wide. The invariant suite (in `tests/test_safety_invariants.py`)
  pins all of it, including a behavioral test that drives a full
  crossing through recording fakes and proves the gate is
  re-consulted during the run — and that a vanished restore point
  means zero installs.
- **The way back is part of the product.** Whenever a crossing
  fails, the summary names the toll's restore point, so the owner
  can return the PC to exactly where it was before the crossing.

## Configuration

Charon has no configuration file and no settings screen — the
standing rules above are not configurable, on purpose. The knobs
that do exist are the command-line options in the table above
(`--ledger`, `--json`, `--from-report`, `--only`, `--debug`), and
the ledger's default location follows the platform's app-state
convention (`%PROGRAMDATA%\Charon` on Windows; `$XDG_DATA_HOME` or
`~/.local/share` elsewhere).

## Development & testing

```
python -m pytest            # the full suite
python -m pytest --collect-only -q
```

The suite stands at **217 tests, verified by collection**, and runs
green on Linux — because it is built to: recorded fixtures
(`tests/fixtures/` — CIM driver JSON, `pnputil` output, Windows
Update offers) and fake/recorded seams stand in for Windows, and the
GUI tests run headlessly under PySide6's offscreen platform (the GUI
test module skips itself where PySide6 isn't installed, so the
engine suite stays green on a Qt-less machine —
`pytest --collect-only` reports 194 in that case). Coverage spans
the inventory and matching logic, download staging and the ledger,
the toll and its skip trap, the apply engine and the stop-the-line
rule, the CLI, the safety invariants, and the window end to end —
including the contrast and no-purple palette pins.

That is testing by design and by fixture. It is **not** the same
claim as verification on real hardware — see the honest status
below.

## Status & roadmap

**v1.0.0 — the five-phase roadmap is complete.** Phases 1–4 were
built 2026-10-19–20 and Phase 5, the animated window, completed the
roadmap on 2026-10-20. The full phase-by-phase record — including
what each phase's audit pinned — lives in
[docs/ROADMAP.md](docs/ROADMAP.md).

**v1.0.1 — the portable edition has SHIPPED (2026-10-20).**
[Charon v1.0.1](https://github.com/TheRealJesusTheHacker/Charon.exe/releases/tag/v1.0.1)
is Charon's first published release, and it carries
`CharonPortable.zip` and its `SHA256SUMS.txt`, built and
attached by the release workflow after the full suite passed
on Windows. v1.0.1 also fixes the Windows concurrent-ledger-
writer lock (every writer now locks byte 0 of the ledger lock
file; concurrent download/toll appends no longer lose a record
on Windows) — see [CHANGELOG.md](CHANGELOG.md). The portable
edition is no longer backlog: the PyInstaller one-folder spec,
the pinned CI dependency list, the portable readme, and the
release workflow are all committed under `packaging/` and
`.github/workflows/` (see [docs/ROADMAP.md](docs/ROADMAP.md)).

**v1.0.2 — the kit hunt (2026-10-20).** A fleet-wide hunt for
defects only a real Windows machine could expose found one in
Charon's download seam: it read each download's verdict from the
downloader object, but Windows Update keeps those verdicts on the
result object its synchronous `Download()` returns — so on a real
PC every staged download would have been reported "not
downloaded", however the fetch itself went. The test fake had
been built to the same mistaken shape, which is why the suite was
green; the fake now models the documented API and regression
tests pin it. The same hunt fixed the window closing mid-step
(it now refuses, in plain language, until the step finishes) and
made the ledger lock's unlocked fallbacks log a warning instead
of proceeding in silence. See [CHANGELOG.md](CHANGELOG.md).

**Pending, stated plainly: Windows on-hardware verification.**
Every phase's engine is proven against recorded fixtures and mocked
Windows seams, with the suite green on Linux. Running the complete
crossing on a real Windows PC — scanning a real machine and
comparing against Device Manager, `pnputil`, and Windows Update's
own offered drivers; staging real drivers; creating a real toll,
including the back-to-back skip-trap case and the protection-off
refusal; installing real drivers behind a real toll, including the
reboot-required and stop-the-line paths; and the window on a real
display — is the next step, and until it is done, no claim here
should be read as "proven on real Windows machines."

What comes after that is only what the roadmap's backlog lists:

- A single self-installing `Charon.exe` (one file, one permission
  prompt, self-verifying setup).
- Driver backup/export — a passenger manifest the owner can keep.
- Scheduled scan reminders ("the ferryman returns monthly").
- A signed update-feed / vendor-catalog expansion beyond Windows
  Update — only if it can be done with verified provenance.

Standing rules, from the roadmap: audit everything (build → bug
hunt → fix → re-audit the fixes, before any release); functionality
first (intended behavior is never stripped to satisfy a scanner);
and the toll is non-negotiable.

## License

Charon is proprietary software of Sentinel Safeguard LLC — see
[LICENSE](LICENSE). Copyright © 2026 Sentinel Safeguard LLC. All
rights reserved; no copying, redistribution, resale,
republication, or derivative works are permitted without prior
written permission from Sentinel Safeguard LLC. The software is
provided "as is," without warranty of any kind.

*Sentinel Safeguard LLC — Kansas City, MO.*
