# Changelog — CHARON.EXE

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
