# Changelog — CHARON.EXE

## v1.0.2 — 2026-10-20

Kit-hunt patch: a fleet-wide hunt for defects that only a real
Windows machine could expose, run before the owner's next
physical test of the portable edition.

- **Download verdicts are now read from the object Windows
  actually puts them on (critical).** In `charon/wua_download.py`,
  the download seam read each update's verdict from the
  downloader (`downloader.GetUpdateResult(...)`) and passed a
  progress sink to the synchronous fetch. The documented WUA API
  (wuapi.h, `IUpdateDownloader`) is different in both places:
  `Download()` takes no argument and **returns** an
  `IDownloadResult`, and the per-update verdicts are read from
  that returned object. On a real PC the old code's verdict reads
  all failed, so every download — however the fetch itself went —
  was recorded and reported as "not downloaded", and the crossing
  could never begin. The seam now calls `Download()` as
  documented and reads verdicts from its return value; the
  progress-sink machinery is gone (progress callbacks belong to
  the separate asynchronous job API this seam does not use, and
  callers already treat progress as a bonus, never a promise).
  The shared test fake had been built to the seam's mistaken
  shape, which is why the suite stayed green; the fake now models
  the documented API, and new regression tests model the real
  downloader shape exactly (no `GetUpdateResult` on the
  downloader, a no-argument `Download()` returning the result).
  Both failed before the fix — locally and on the Windows runner.
- **Update collections are now built the way Windows builds
  them (critical).** Both batch seams asked the session for a
  collection (`session.CreateUpdateCollection()`), but
  `IUpdateSession` has no such method — a collection is its own
  coclass, `Microsoft.Update.UpdateColl`. On a real PC that call
  fails, so the download and install steps would each have
  refused at setup, before moving a single byte. Both seams now
  build the collection through that coclass (injectable in
  tests, like the session). The shared fake no longer offers the
  phantom session method, a safety invariant now bans it from
  the package outright, and a Windows-only probe test pins both
  halves of the real shape against the live API.
- **The window refuses to close mid-step.** Closing the window
  while an engine worker thread was running was accepted, and
  destroying a running worker thread makes Qt abort the whole
  process — a crash, mid-scan or mid-crossing. The window now
  refuses the close in plain language ("let it finish before
  closing the window") until the step completes. Regression
  test included (failed before the fix on Linux and Windows).
- **The ledger lock no longer falls silent.** In
  `charon/download.py::_ledger_lock`, two paths proceeded to
  write unlocked without a word — the lock file failing to open,
  and the Windows retry budget running out under contention.
  Writing unlocked remains the designed fallback (a lost race
  costs history, never safety), but both paths now log a warning,
  like the lock-acquisition failure path already did. Regression
  test included (failed before the fix on Linux and Windows).
- **Probed and cleared, with evidence:** creating the Windows
  Update COM session on the GUI worker thread works on the
  Windows runner (a suspicion the hunt raised; a permanent
  Windows-only probe test now guards it), and the v1.0.1 ledger
  lock fix was re-probed adversarially (seed-byte, offset, and
  unlock paths) — no sibling defect found there.

## v1.0.1 — 2026-10-20

- **The portable edition ships.** This is the first release to
  carry `CharonPortable.zip` — the PyInstaller one-folder build
  of Charon (the HUD app, no install, no Python on the target
  machine) plus its portable readme — with `SHA256SUMS.txt`
  covering the zip, built and attached by the release workflow
  after the full test suite, the lazy-import warn-file gate, and
  an offscreen launch smoke test of the packaged exe all pass
  on Windows.
- **Windows concurrent-ledger-writer lock fix.** Concurrent
  writers to Charon's shared ledger (download runs and toll
  records) could lose an append on Windows: 19 of 20 concurrent
  appends landed in
  `tests/test_toll.py::test_concurrent_ledger_writers_lose_nothing`.
  In `charon/download.py::_ledger_lock`, the Windows branch
  wrote a seed byte to an empty lock file before calling
  `msvcrt.locking`, leaving that writer's file offset at byte 1
  while every other writer locked byte 0 — two writers could
  hold "the lock" on different bytes and race. Every writer now
  rewinds to offset 0 before locking (and before unlocking), so
  all writers contend for the same byte. The POSIX `flock`
  path is unchanged. The test now passes on Windows
  (215 passed, 2 skipped in the release workflow's dry run).
- **Release-gate correction.** The workflow's PyInstaller
  warn-file gate no longer fails on a line that merely mentions
  a watchlist module (for example the benign missing
  `win32com.gen_py` generated-cache submodule, while `win32com`
  itself was found); it fails only when an exact watchlist
  module (`win32com`, `pythoncom`, `pywintypes`, `PySide6`) is
  itself missing.

## v1.0.0 — 2026-10-20

- The five-phase roadmap is complete: driver inventory & scan
  engine, Windows Update download staging, the toll (a restore
  point created and verified before any update is applied),
  the apply engine, and the animated HUD window. v1.0.0 was a
  source-only release state — no GitHub release or portable
  artifact was published for it; the portable edition's
  packaging was committed alongside it and ships in v1.0.1.
