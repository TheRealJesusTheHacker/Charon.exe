# CHARON.EXE — Build Roadmap

The ferryman's chart. Phases run in order; the toll (Phase 3) is designed
alongside the apply engine from day one, never bolted on after.

**The Crossing (fixed workflow, never reordered):**
Scan → Download → **Restore point (the toll)** → Apply.

**Safety law:** no restore point, no update. A failed or unverifiable toll
stops the run before anything is applied — by design, not by error handling.

---

## Phase 1 — Driver inventory & scan engine

- Enumerate the Windows Driver Store and installed device drivers
  (`pnputil /enum-drivers`, `Win32_PnPSignedDriver`, SetupAPI data).
- Per driver: device, provider, version, driver date, class, signer status.
- Baseline snapshot of the machine's driver state (the "passenger manifest").
- Outdated determination: installed version/date vs. what Phase 2 can source.
- Read-only by design — Phase 1 never changes a driver.

**Status: BUILT (2026-10-19).** The scan engine is complete:

- `charon/platform_inventory.py` — the only OS seam: the read-only CIM
  device query (`Win32_PnPSignedDriver` via PowerShell → JSON) plus the
  `pnputil /enum-drivers` enumeration as cross-check and fallback.
- `charon/wua.py` — a search-only Windows Update Agent seam (the asking
  half of Phase 2's sourcing, built now because outdated determination
  needs it). It exposes **no** download or install capability at all.
- `charon/inventory.py` / `charon/matching.py` — pure logic: normalize
  both sources into one passenger manifest, match devices to offered
  updates by hardware ID (name-only matches are labeled lower
  confidence), classify `update_available` / `up_to_date` / `unknown` —
  never a guess. Version comparison outranks dates; date-only calls are
  low confidence, because driver dates lie.
- `charon/model.py` / `charon/cli.py` — the report model and
  `python -m charon scan` (table + `--json` export), with designed
  plain-language failure states instead of tracebacks.
- Safety is test-pinned: the suite asserts the exact OS commands
  issued and scans the package to prove no code path can download,
  install, remove, or modify a driver — and none creates restore
  points yet (the toll is Phase 3, on schedule, not early).
- 72 tests green on Linux from recorded fixtures. **Pending: Windows
  on-hardware verification** (scan aroura; compare against Device
  Manager, pnputil, and Windows Update's own offered drivers).

## Phase 2 — Update sourcing

- Primary channel: the **Windows Update Agent API** — the legitimate route
  by which Microsoft itself offers driver updates for the installed hardware.
- Match offered updates to the Phase 1 manifest; report honestly when no
  update exists for a device (an old driver with no successor is a fact,
  not a failure).
- **Never** scrape third-party "driver download" sites: wrong-driver and
  malware risk, and beneath the product's standard.
- Downloads staged to a Charon-owned folder, hash-verified where the
  channel provides hashes, applied to nothing yet.

## Phase 3 — Restore-point integration (THE TOLL)

- Detect whether System Restore is enabled for the system drive; if it is
  off, guide the owner to enable it in plain language — Charon does not
  cross without the toll, and does not silently flip system settings.
- Create the restore point (System Restore via WMI/`SRSetRestorePoint`
  family of APIs) named for the run: date, time, and "CHARON.EXE" so it is
  findable later by a human.
- **Verify** the restore point actually exists and is listed before the
  apply phase unlocks. "Creation requested" is not "toll paid."
- Mind Windows' restore-point frequency limits (a recent point may make a
  new one silently skipped by the OS) — verification must catch this; a
  skipped toll is an unpaid toll.

## Phase 4 — Apply engine

- Staged installs, **one driver at a time**, from the verified downloads
  (`pnputil /add-driver … /install` and the device-install APIs), never a
  bulk blast.
- After each install: confirm the device reports healthy; a device that
  comes up broken stops the line — remaining drivers are not applied.
- Reboot handling: if a driver needs a restart, say so plainly, save the
  run state, and resume/verify after the reboot.
- Failure path: stop, explain in plain language, and guide the owner to
  the toll restore point from Phase 3. The way back is part of the product.

## Phase 5 — The animated UI

- PySide6, Sentinel green-and-black, premium and futuristic — a product
  people screenshot: HUD-style animated scan sweep over the driver
  manifest, the river-crossing journey visual through Download → Toll →
  Apply, animated per-driver progress.
- Every state is a designed screen: scanning, updates found / none found,
  downloading, **toll paid** (restore point confirmed, shown like a
  stamped ticket), crossing (applying), a driver failed (ferry holds
  position, way back offered), done.
- Plain language only. No raw OS errors, no codes without an explanation
  and one clear next action.
- Same flagship standard as Argus.exe: no light backgrounds, contrast
  verified, every control functional.

## Later / backlog

- Single self-installing `Charon.exe` (the Argus v1.1.0 pattern: one file,
  one permission prompt, self-verifying setup) once the engine phases land.
- Portable edition for the Sentinel IT Toolkit USB (with NitroCore
  Portable and Argus Portable).
- Driver backup/export (a "passenger manifest" the owner can keep).
- Scheduled scan reminders ("the ferryman returns monthly").
- Signed update-feed / vendor-catalog expansion beyond Windows Update,
  only if it can be done with verified provenance.

## Standing rules

- **Audit everything**: build → bug hunt → fix → re-audit the fixes,
  before any release.
- **Functionality first**: intended behavior is never stripped to satisfy
  a scanner; document-and-dismiss instead.
- **The toll is non-negotiable** and outranks every convenience feature.
