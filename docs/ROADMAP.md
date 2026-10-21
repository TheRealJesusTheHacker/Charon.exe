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

## Phase 2 — Update sourcing & download staging

- Primary channel: the **Windows Update Agent API** — the legitimate route
  by which Microsoft itself offers driver updates for the installed hardware.
- Match offered updates to the Phase 1 manifest; report honestly when no
  update exists for a device (an old driver with no successor is a fact,
  not a failure).
- **Never** scrape third-party "driver download" sites: wrong-driver and
  malware risk, and beneath the product's standard.
- Downloads are staged, applied to nothing yet. Staging truth: WUA
  `Download()` stages into the **Windows Update cache** — the channel
  offers no Charon-owned target folder, and this plan no longer
  pretends otherwise. Charon's own **download ledger** is the record
  of what is staged.

**Status: BUILT (2026-10-19).** Download staging is complete:

- `charon/wua_download.py` — the one download seam: builds the WUA
  downloader, downloads a batch, reads every per-update verdict
  individually (succeeded / succeeded-with-errors / failed / aborted).
  An update whose license terms require acceptance gets them accepted
  explicitly, and the acceptance is recorded per update. A "success"
  Windows Update claims but didn't stage counts as failed. There is
  no installer capability in this module — or anywhere in the package.
- `charon/download.py` — pure logic: the download plan (from a scan
  report's outdated entries, with explicit subset selection for the
  future UI's driver picker), **revalidation against a fresh search**
  run moments before the fetch — an update that no longer resolves is
  marked `no_longer_available` and *nothing is downloaded in its
  place* — the progress-event stream Phase 5 will animate, and the
  download ledger (atomic writes; per update: title, version, device,
  result, EULA acceptance, size, timestamp).
- `python -m charon download` — fresh scan in-session → plan →
  revalidate → stage → ledger + an honest summary ("3 of 4 drivers
  downloaded; the Bluetooth driver failed — nothing was installed").
  `--from-report <scan.json>` stages from an exported scan (still
  revalidated fresh). Exit code 2 only when a download couldn't begin.
- Safety invariants v2: installer machinery and restore-point
  machinery remain forbidden package-wide; download capability is
  source-pinned to its one seam; a behavioral test drives a full
  staged download through recording fakes and pins the exact
  search + download call sequence with zero install/restore calls.
  The scan path is byte-for-byte as read-only as Phase 1 proved it.
- Test suite green on Linux from fake WUA seams. **Pending: Windows
  on-hardware verification** (stage real drivers on aroura; confirm
  Device Manager versions unchanged, ledger matches the cache, an
  interrupted download is reported honestly).

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

**Status: BUILT (2026-10-19).** The toll machinery and the gate are
complete:

- `charon/toll.py` — the only restore-point seam: a pinned PowerShell
  probe (administrator? System Protection state, read from shadow
  storage), `Checkpoint-Computer` creation, and enumeration, all
  behind one runner. `charon toll` pays the toll standalone and
  prints the receipt.
- **Verification is enumeration, never trust.** A point pays the toll
  only if it carries this run's unique marker (run timestamp + token,
  so yesterday's point can never be passed off as today's) AND is
  stamped no earlier than this run's creation call (2s of clock
  tolerance — a point is stamped while the call runs). Windows' 24-hour
  silent skip therefore reads exactly as what it is: **unpaid**. The
  skip trap is caught by design and pinned by test.
- Preconditions are designed states, detected before creation:
  System Protection off → plain-language refusal with the how-to —
  and Charon never flips that setting itself; not-an-administrator →
  plain-language refusal. Protection state the probe cannot determine
  is not guessed at: creation is attempted and verification decides.
  Every failure mode — skipped, undatable, unreadable, unrecordable —
  resolves to `TollUnpaid`. The ferry fails toward not moving.
- `require_paid_toll()` is the gate Phase 4 must call first, every
  time: the candidate record (session, else the ledger — the toll is
  recorded in the same ledger Phase 2 keeps) is **re-verified against
  the PC** before the gate opens; a deleted point closes it again.
- Safety invariants were rewritten in the open for this phase:
  restore-point capability is confined to `toll.py`, install
  capability remains forbidden everywhere. 26 toll tests green on
  Linux (fake seam + recording runner, argv pinned byte-for-byte);
  the full suite stands at 131. **Pending: Windows on-hardware
  verification** (see the phase report: real creation, the two-points-
  back-to-back skip-trap test, the protection-off refusal, timing).

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

**Status: BUILT (2026-10-20).** The crossing is complete:

- `charon/wua_install.py` — the one install seam: the Windows Update
  installer, one update per call (a collection of exactly one), every
  verdict read individually. A success Windows Update claims but the
  PC doesn't show counts as failed. A restart it asks for is reported
  in the outcome — the seam has no way to restart a PC, ever. (The
  crossing ships through the same Windows Update channel that
  sourced and staged the updates — the staged cache is consumed by
  that channel's installer; the pnputil route above stays forbidden
  by the safety invariants, since the cache hands out no INF paths
  to feed it.)
- `charon/apply.py` — pure logic: the staged set comes from the
  ledger (exactly Phase 2's staged rows, minus crossings already
  applied); administrator rights confirmed; **every staged update
  re-validated against a fresh search** (a withdrawn or swapped
  update is `no_longer_available`, never substituted); then
  **`require_paid_toll()` runs fresh, inside the apply, moments
  before the first install** — the gate→apply gap the Phase 3 audit
  recorded is closed by construction, not by caching a "paid" flag.
  Unaccepted license terms block their driver alone
  (`eula_not_accepted`). After each crossing the device is checked
  (no longer offered as not-installed, still reporting in, carrying
  the new version); a failed install or an unhealthy device stops
  the line — the rest are recorded as held back. Every verdict is
  written to the ledger's `apply_runs` through the same locked
  writer, and the summary names the toll's restore point as the
  way back whenever a crossing failed.
- `python -m charon apply` — the composed pipeline with designed
  plain-language states for every refusal (gate, admin, search,
  install-unavailable, ledger).
- Safety invariants rewritten in the open for this phase: install
  capability confined to `wua_install.py`; restart machinery
  forbidden everywhere; a behavioral pin drives a full crossing
  through fakes and proves the gate is re-consulted during the run
  and that a vanished point means zero installs. 42 new tests;
  the suite stands at 193. **Pending: Windows on-hardware
  verification** (real installs behind a real toll on aroura;
  reboot-required flow; the stop-the-line path on a live device).

## Phase 5 — The animated UI

**Status: BUILT (2026-10-20) — v1.0.0. The five-phase roadmap is
complete.** The animated window is built:

- PySide6, Sentinel green-and-black, premium and futuristic — a product
  people screenshot: HUD-style animated scan sweep over the driver
  manifest, the river-crossing journey visual through Download → Toll →
  Apply, animated per-driver progress — all of it animated from the
  engine's own progress events, on a worker thread, so the screen only
  ever shows work actually happening.
- One window, one journey: `charon/gui.py` drives the same composed
  pipeline as the CLI. The interface owns no capability of its own —
  no COM, no commands — pinned by the invariant suite. The crossing
  stays locked until this visit's toll verifies and the ticket stamps
  **TOLL PAID** — and the engine still re-verifies the gate behind
  the button; a gate that closes mid-flow returns the owner to the
  toll screen with the reason.
- Every state is a designed screen: scanning, updates found / none found,
  downloading, **toll paid** (restore point confirmed, shown like a
  stamped ticket), a toll refused (with the plain-language way
  forward), crossing (applying), a driver held back, a restart needed
  (reported, never forced), done.
- Plain language only. No raw OS errors, no codes without an explanation
  and one clear next action.
- Same flagship standard as Argus.exe: no light backgrounds, every
  text pairing pinned at ≥ 4.5:1 contrast by test, no purple anywhere
  in the palette (also pinned), every control functional. GUI tests
  run headlessly in the suite; audit screenshots of the main states
  live in `phase5-audit/` locally.
- **Pending: Windows on-hardware verification** — the window on a
  real display and the full pipeline against a real PC on aroura.

## Portable edition — committed deliverable

**Status: SHIPPED (2026-10-20) — v1.0.1.** The portable edition
shipped in
[Charon v1.0.1](https://github.com/TheRealJesusTheHacker/Charon.exe/releases/tag/v1.0.1),
Charon's first published release, which carries
`CharonPortable.zip` and `SHA256SUMS.txt` as its assets. Cutting
it required fixing the Windows concurrent-ledger-writer lock
in `charon/download.py::_ledger_lock` (every writer now locks
byte 0; the failing Windows test
`test_concurrent_ledger_writers_lose_nothing` passes — 215
passed, 2 skipped in the release workflow) — see
`CHANGELOG.md`. The portable edition is no longer backlog; it
is a shipped deliverable with its build wired end to end:

- `packaging/Charon.spec` — a PyInstaller one-folder build of the
  window (the `charon` package plus its PySide6 and pywin32 COM
  runtime in one folder: no install, no Python on the machine).
- `packaging/launch_charon.py` — the frozen entry point: no
  arguments opens the window; any argument routes to the same CLI
  as `python -m charon`.
- `packaging/requirements-ci.txt` — the release build's pinned
  dependency list.
- `packaging/portable/README-PORTABLE.txt` — ships inside the zip,
  including the honest differences from running from source (the
  ledger and restore points stay with the machine they describe).
- `.github/workflows/build-release.yml` — on every published
  release: tag-vs-version check, the full test suite, the portable
  build, the lazy-import warn-file check, an offscreen launch
  smoke test of the packaged exe, then `CharonPortable.zip` and
  `SHA256SUMS.txt` attached to the release.

The portable edition is Charon's seat in the Sentinel IT Toolkit
USB, alongside NitroCore Portable and Argus Portable.

## Phase 6 — Driver backup/export (the passenger manifest)

The one backlog item that completes the existing workflow rather
than adding a new one: the toll (restore point) protects the
crossing; the manifest is the cargo record of what was carried.

**Status: BUILT (2026-10-21) — v1.0.5.**

- `charon/manifest.py` — pure logic plus the one file write the
  owner asks for: it assembles the keepable manifest from the scan
  report and the ledger's crossing record, and writes it
  atomically. The driver rows are the scan report's own
  serialization — the manifest may not paraphrase the cargo — and
  every count is computed from the rows the manifest carries.
- The manifest holds: every driver the scan found (device,
  provider, version, date, class, signer status, the update
  Windows Update offered), plus — when the ledger has them — the
  download records, the tolls paid (restore point numbers and
  times), and the apply records of what crossed.
- Two forms: JSON (the full record) and a readable text document
  for keeping or printing. `python -m charon manifest --out
  <file>` (fresh scan, or `--from-report <scan.json>`), `--format
  text` for the readable form, `--ledger` for the crossing
  record's source. The window carries the same export as the
  manifest page's **Save passenger manifest** button (saved to
  the owner's Documents folder), driven through the same composed
  pipeline as every other engine call.
- Honesty, pinned by test: a scan that could not hear from every
  source produces a manifest marked **not complete**, with the
  scan's warnings carried verbatim; a ledger that cannot be read
  flags the crossing record the same way — a partial manifest is
  never dressed as complete. A failed export is a designed
  failure and leaves no partial file at the destination (and
  never touches a good manifest already saved there).
- The toll is untouched by this phase: the manifest reads records
  and writes one export file. It creates no restore point, asks
  Windows Update nothing, and the apply gate — including
  v1.0.4's re-proof before every install — is exactly as shipped.
  Sourcing remains the Windows Update Agent API only.
- **Pending: Windows on-hardware verification** — the same list
  as the other phases: an exported manifest from a real scan on
  aroura, checked against Device Manager and the ledger.

## Later / backlog

- **Single self-installing `Charon.exe` — CLOSED (2026-10-21).**
  Portable-only is the decided form: the portable one-folder
  edition is the owner's portable mandate already shipped — a
  second installer artifact adds sync burden and an
  unsigned-installer surface for zero added capability.
- **Scheduled scan reminders ("the ferryman returns monthly") —
  CLOSED (2026-10-21).** Drivers do not expire monthly and
  Windows Update already reminds; a reminder surface is scope
  in search of a need.
- Signed update-feed / vendor-catalog expansion beyond Windows Update,
  only if it can be done with verified provenance.

## Standing rules

- **Audit everything**: build → bug hunt → fix → re-audit the fixes,
  before any release.
- **Functionality first**: intended behavior is never stripped to satisfy
  a scanner; document-and-dismiss instead.
- **The toll is non-negotiable** and outranks every convenience feature.
