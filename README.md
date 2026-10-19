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

**Phase 1 — scan engine built** (2026-10-19). `python -m charon scan`
inventories every installed driver and flags the outdated ones against
what Windows Update itself offers — read-only: nothing is downloaded,
installed, or changed. A 72-test suite (recorded fixtures) is green;
Windows on-hardware verification comes next. Phases 2–5 — download
staging, the toll, the apply engine, the animated UI — follow the
roadmap, in the Crossing's order.

*Sentinel Safeguard LLC — Kansas City, MO.*
