# Changelog — CHARON.EXE

## v1.0.4 — 2026-10-21

Fleet sweep #3: a full bug, function, and security hunt over
the whole tree, with fixes. One change strengthens the toll
itself; the rest are smaller honesties. The crossing's order —
Scan → Download → Restore point → Apply — is unchanged, and
driver sourcing is still the Windows Update Agent API only.

- **The toll is now re-proved before every single install.**
  The gate already ran fresh inside the apply, moments before
  the first install. But a crossing can take minutes per
  driver, and a restore point can vanish inside that window —
  Windows itself prunes the oldest points when disk space runs
  short. The apply now re-verifies the toll against the live
  PC immediately before each install; if the point can no
  longer be confirmed, that driver and everything behind it
  are recorded as held back, and nothing more is installed on
  a way back that cannot be proved. Two new tests drive the
  point vanishing mid-crossing and in the breath between the
  gate and the first install: zero further installs, both
  verdicts in the ledger.
- **The post-crossing device check judges the right device.**
  With two devices sharing a name and no device ID to tell
  them apart, the check used to run on to the *last* namesake
  and could judge a crossing by a different device's version.
  An ID match anywhere in the list now wins, and the name
  fallback takes the first match.
- **Download events speak the ledger's words.** A download
  result in a vocabulary Charon doesn't know is counted as
  failed everywhere — the completion event now carries that
  same normalised verdict, instead of repeating the seam's raw
  word to screens while the ledger said "failed".
- **Model matching needs whole words.** "net" no longer
  matches inside "network", nor "audio" inside "audiophile":
  model containment counts only at word boundaries, so one
  device is not offered another's driver on a shared fragment.
- **A finished window worker drops its callable.** The worker
  stays a child of the window after its step; it no longer
  keeps the step's callable (and whatever it closed over,
  including live Windows Update objects) alive with it.

Suite: 234 passed, 2 skipped (offscreen, Qt present) — the
229 of v1.0.3 plus five new pins. Still unproven on real
hardware, exactly as before: the whole crossing on a real
Windows PC remains the owner's verification list, unchanged.

## v1.0.3 — 2026-10-20

Charon gets its own face. Every tool in the medic kit now wears
the colors of what it was named for; Charon's are the Styx — a
charcoal-slate night, the river's aqua-teal for everything that
moves or can be pressed, and the muted gold of the obol, the
coin paid for passage, for the moments that carry a warning.

- **Colors only.** The change surface is the palette: the
  constants in `charon/theme.py`, the styles they feed in
  `hud.py` and `gui.py`, and the tests that pin them. No engine
  file changed — the hunt's Windows Update seams, the ledger
  lock stack, the close-while-busy refusal, and the toll gate
  are exactly what v1.0.2 shipped, and no behavior, layout, or
  wording changed anywhere.
- **Every pairing pinned, the identity pinned.** The theme
  tests hold each rendered text pairing at WCAG 4.5:1 or better
  (the worst is retired-control text on a panel, 5.88:1) and
  now pin the palette's exact values, so the identity cannot
  drift — or be "helpfully" recolored — unnoticed. The
  no-purple pin still holds.
- **Danger reads as danger.** The toll-refusal banner and the
  held-back driver chip are now white text on the kit's shared
  danger red itself, instead of red text on a dark panel — the
  one treatment the whole kit uses, impossible to miss.
- **The screenshot audit was re-run end to end** — all seven
  main states re-rendered in the new palette and checked:
  primary actions in river aqua with dark text, the TOLL PAID
  stamp struck in aqua, the refusal banner in full danger red,
  no light backgrounds, no painted purple.

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
- **The ledger lock race is closed for real this time.** The
  first v1.0.2 release run's Windows gate caught the v1.0.1
  concurrent-writer race still alive — 19 of 20 appends landed,
  and the captured log showed why: a writer that finds the lock
  file empty writes the seed byte at offset 0, the very byte a
  sibling may already hold locked, and Windows refuses a write
  into a locked region from another handle. That refusal landed
  in the designed proceed-unlocked fallback, and the unlocked
  writer raced. Two changes close it in
  `charon/download.py::_ledger_lock`: threads of the same
  process now queue on a per-path process lock before touching
  the OS lock (byte locks arbitrate between processes), and the
  seed write retries until the sibling's seed is visible instead
  of falling through. Two deterministic regression tests pin
  both halves (both failed before the fix), the concurrent
  writers test was joined by a 200-append stress variant, and
  the failed first cut of this release was deleted and re-cut.
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
