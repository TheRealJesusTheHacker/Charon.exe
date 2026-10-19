# CHARON.EXE

**Sentinel Safeguard LLC — Windows Driver Updater**

*Every driver ferried safely to the other side.*

---

## The Ferryman

In the old stories, Charon ferried souls across the river Styx — and nobody
crossed without paying his toll. One coin, paid up front, no exceptions.

CHARON.EXE is Sentinel Safeguard's ferryman for Windows drivers. It carries
every driver on a PC from the old version to the new one — carefully, one
crossing at a time — and the ancient rule still holds:

> **Nobody crosses without paying the toll.**
> **The toll is a restore point** — created and verified *before* a single
> update is applied. Every time. No exceptions.

If the toll can't be paid, the ferry doesn't move. That's not a setting.
That's the law of the river.

## The Crossing

Every update run follows the same four steps, in this order, always:

1. **Scan** — Inventory every installed driver and flag the outdated ones.
2. **Download** — Fetch the updates. Nothing is applied yet.
3. **The Toll** — Create a System Restore point and verify it exists.
   The crossing does not begin until the toll is confirmed paid.
4. **Apply** — Install the updates, one at a time, verifying the device
   still works after each crossing.

## Why a driver updater needs a ferryman

Driver updates are where ordinary PCs go to die: a bad network, display, or
storage driver can leave a machine unbootable or offline, and the people it
happens to are usually the ones least able to recover from it. Charon's whole
design answers that: know what's installed, fetch only legitimate updates,
**pay the toll first**, cross one driver at a time, and always keep the way
back open.

## The kit

Charon joins Sentinel Safeguard's medic-kit line:

- **Argus.exe** — antivirus & malware detection ("all eyes, all detection")
- **NitroCore** — system performance
- **VectorZip** — file compression
- **Charon.exe** — driver updates, with the toll paid first

## Build plan

Detailed phases live in [docs/ROADMAP.md](docs/ROADMAP.md):

1. **Driver inventory & scan engine** — enumerate installed drivers,
   versions, dates, providers; flag what's outdated.
2. **Update sourcing** — match installed drivers to legitimate updates
   through the Windows Update channel. Never random driver sites.
3. **Restore-point integration** — the toll: create and verify a System
   Restore point as a hard gate before anything is applied.
4. **Apply engine** — staged, one-at-a-time installs with per-driver
   verification and a guided way back when a crossing fails.
5. **The animated UI** — a premium, futuristic HUD-style interface:
   animated scanning, the river-crossing journey through download, toll,
   and apply. Plain language throughout, designed screens for every
   failure state — a toll that can't be paid is a docked ferry, not an
   error code.

## Usage (what runs today)

Charon is in development and runs from source — there is no installer
yet (a single self-installing `Charon.exe` is on the roadmap's backlog,
once the engine phases land). The scan, download staging, and the toll
are built:

```
python -m charon scan              # inventory drivers, flag outdated ones
python -m charon scan --json PATH  # also write the full report as JSON
python -m charon download          # stage outdated drivers (installs nothing)
python -m charon toll              # pay the toll: create + verify a restore point
python -m charon --version         # 0.4.0
```

The scan is **read-only**: it downloads nothing, installs nothing, and
changes nothing. Download stages updates into the Windows Update cache
and applies nothing. The toll creates a System Restore point and
verifies it exists. The inventory runs on Windows (it queries the
Windows driver store and Windows Update); the test suite runs
anywhere from recorded fixtures.

## How it works

- `charon/platform_inventory.py` is the only OS seam: a read-only CIM
  device query (`Win32_PnPSignedDriver` via PowerShell, returned as
  JSON) plus `pnputil /enum-drivers` enumeration as cross-check and
  fallback.
- `charon/wua.py` is a search-only Windows Update Agent seam — it asks
  what Windows Update itself offers for the installed hardware. It
  exposes no download or install capability at all.
- `charon/inventory.py` and `charon/matching.py` are pure logic: both
  sources are normalized into one passenger manifest, devices are
  matched to offered updates by hardware ID (name-only matches are
  labeled lower confidence), and each driver is classified
  `update_available` / `up_to_date` / `unknown` — never a guess.
  Version comparison outranks dates; date-only calls are low
  confidence, because driver dates lie.
- `charon/download.py` and `charon/wua_download.py` stage the updates:
  every plan is revalidated against a fresh search (a withdrawn update
  is marked `no_longer_available` — never silently substituted), the
  fetch lives in the one download seam, and the ledger records what
  is aboard. Nothing is installed.
- `charon/toll.py` is the toll seam and the gate: it creates the
  restore point with a marker unique to the run, then **verifies by
  enumeration** that a point carrying that marker was freshly stamped
  — Windows silently skips a requested point when a recent one exists,
  and a skipped toll is an unpaid toll. `require_paid_toll()` is the
  gate the apply engine must pass, re-verified against the PC every
  time.
- `charon/model.py` and `charon/cli.py` produce the report (table or
  JSON) with designed plain-language failure states instead of
  tracebacks. The safety properties are test-pinned: the suite asserts
  the exact OS commands issued and proves capability is confined to
  its seam — no code path can install, remove, or modify a driver
  today, and restore points exist only in the toll seam.

## Standards

Built to the Sentinel bar, same as the rest of the kit:

- **Premium, no corners cut** — futuristic look and feel is a requirement,
  not decoration.
- **Functional first** — every control does real work; no mock buttons,
  no placeholder actions.
- **Plain language** — made for non-technical owners and Sentinel's
  clients. No jargon on screen.
- **Audit everything** — every build is hunted, fixed, and re-audited
  before release. Remain vigilant, like a sentinel.

## Status

**Phases 1–3 built** (2026-10-19). `python -m charon scan` inventories
every installed driver and flags the outdated ones against what
Windows Update itself offers — read-only. `python -m charon download`
stages those updates into the Windows Update cache (never a silent
substitute: every plan is re-checked against a fresh search first)
and records them in Charon's download ledger — **nothing is
installed**. `python -m charon toll` pays the toll: a System Restore
point created and then **verified** by enumeration — because Windows
silently skips a requested point when a recent one exists, Charon
trusts only a point carrying this run's own marker, freshly stamped.
A skipped, missing, or unverifiable toll is an unpaid toll, and the
apply engine will be required to present a verified toll at
`require_paid_toll()` before anything crosses. The full test suite
(131 tests, recorded fixtures and fake Windows seams) is green;
Windows on-hardware verification comes next. Phases 4–5 — the apply
engine and the animated UI — follow the roadmap, in the Crossing's
order.

## License

Charon is proprietary software of Sentinel Safeguard LLC — see
[LICENSE](LICENSE). All rights reserved; no copying, redistribution,
resale, or derivative works without prior written permission from
Sentinel Safeguard LLC.

*Sentinel Safeguard LLC — Kansas City, MO.*
