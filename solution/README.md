# Route chainage catalog + inspection coverage facts

Python 3.11 / SQLite. Synthetic data only — no real railway standard is
involved. The route is a physical axis `[0, L)` tiled by positive-length
half-open segments; labels within a segment are `label = physical -
physical_start + label_start` (slope fixed at 1). Labels are **not** a
global coordinate: they may repeat and may have gaps.

## Why labels cannot be a continuous coordinate

The materials' r1 route:

| section | physical `[start, end)` | label `[start, end)` |
|---------|-------------------------|----------------------|
| P1      | `[0, 400)`              | `[0, 400)`           |
| P2      | `[400, 700)`            | `[300, 600)`         |
| P3      | `[700, 1000)`           | `[650, 950)`         |

* label `350` has **two** physical candidates: P1@350 and P2@450. Treating
  `350` as one coordinate would silently swap the inspected object.
* label `620` is a **gap** (P2 ends at 600, P3 starts at 650): unresolved.
* `620` resolves only after the r2 base correction (P2 label base 300 → 330
  widens P2's labels to `[330, 630)`).

The point reverse lookup therefore returns **all** candidates and never
picks a nearest value:

| candidates | status       |
|------------|--------------|
| 0          | `unresolved` |
| 1          | `resolved`   |
| >1         | `ambiguous`  |

A caller may pass an explicit `section_id` to disambiguate; an unknown
section is reported `unresolved`, never guessed.

## Half-open semantics (the subtle part)

Point membership and interval endpoints are treated differently, on purpose:

* A physical **point** exactly on a segment's right end does **not** belong
  to that segment (`start <= p < end`).
* An interval's **upper bound** may equal a segment's exclusive end label.
  Converting a span never re-tests point membership for its upper bound, so
  a legal end-of-section span is accepted. The resulting physical interval
  is still half-open, which correctly excludes a facility sitting exactly
  at the boundary.

The `merge_intervals` union merges overlapping **and touching** intervals,
so `sum(length)` is the true covered measure — repeated or overlapping
spans are never double counted.

## Facilities

Each facility has a stable `asset_id` and a fixed **physical** position that
does not move when a label base changes. A facility counts as covered only
when its physical point lies in the merged physical coverage
(`start <= p < end`). Facility hits are **never** derived from a label
point lookup, so an ambiguous or unresolved label can never manufacture a
hit.

## Independent catalog vs. inspection facts

`chainage_catalog.py` knows nothing about jobs or reports: it can resolve
points and convert intervals on its own. `inspection_library.py` depends on
it, not the reverse. Storage is two separate schemas:

* **catalog**: `route_revision`, `route_section` (exact rationals stored as
  `n/d` text plus a sortable key for indexed range prefiltering).
* **inspection facts**: `asset`, `report`, `report_span`,
  `report_interval`, `report_facility`, `applied_op`.

Reports record the original label request, the selected section, the actual
physical coverage, the covered facility identities, and the catalog
revision. `report_id` is content-addressed: identical content is idempotent,
differing content under the same id is rejected.

## Revision behaviour

`revise_chainage` changes **only** the named section's `label_start`
(honouring the material's explicit base correction). Physical extents,
section order, and every other section are copied verbatim — this is not a
route editor, and no other edits are inferred.

After `r2` (P2 base 300 → 330), the *same* request `P2 [350,390)`:

* r1: physical `[450, 490)` → covers **B**@470 (not C@440).
* r2: physical `[420, 460)` → covers **C**@440 (not B@470).

The facility identities and physical positions are unchanged; only the
label→physical mapping moved. Old report R1 keeps explaining the original
labels and positions (`stale=true` once r2 is current).

## Running

```bash
python3 solution/run_demo.py                    # chainage/inspection (in-memory)
python3 solution/run_demo.py --db route.db      # durable (resumable)
python3 solution/run_market_demo.py             # flag flow -> appointments -> rental
python3 -m unittest discover -s solution/tests -v
python3 solution/bench_scale.py                 # 20 sections / 500 assets / 200 jobs
python3 solution/bench_market.py                # briefing envelope (6 groups/8 cells/4 machines)
```

`run_demo.py` reads `materials/730` operation by operation, applies the P2
base correction, and prints the real facility change (B in r1 → C in r2).
A file-backed DB records an op-log so re-running resumes (mutating ops are
skipped, read-only ops still render).

## Market cue-flag flow → number-taking → rental

`market_flow.py` and `rental_optimizer.py` sit **on top of** the chainage /
inspection library and leave it untouched. They model the fair end to end.

### Sight lines and inspection gating (`market_flow.py`)

* Every cue flag (引客旗) and street-corner partition (街角隔板) is a **named
  physical object**; only objects *actually inspected this session* are trusted.
* A stop (步行停点) faces ±1 on the route axis and sees flags **in front, up to
  the first *inspected* partition**. A flag is **recognised** only when the
  **whole chainage segment** it lives on was inspected (`Flag.segment`), not
  merely its point — checking the point alone would let a same-number flag on a
  different chain-broken segment slip in.
* If a visible flag's segment is uninspected the group **defers** at that stop;
  it may never substitute a same-number flag outside the segment.
* Correcting an inspected partition's physical position to *in front* of a flag
  hides the flag (no ticket, no appointment, no rental).

### Whole-group taking

At each stop a group tries recognised flags by **colour priority → along-path
distance → physical number**. It takes the **whole group's** slots from a
**single** flag or nothing (no splitting, no second number). Same-cell arrivals
are served in registered `fixed_order`; on success the group leaves the path for
the stall, on failure it walks on. A group that is willing to wait
(`max_wait_at_stop`) waits for a number bag that is still en route.

### Two issuance protocols

* `SEALED` (方案一): a named real-number segment is sealed to a group up front;
  other groups cannot take it, and only the guide returning the physical ticket
  (`released_seals`) frees it. Sealed numbers are still gated by physical
  arrival (`open_cell`) — while "still en route" even the holder cannot take
  them.
* `COURIER` (方案二): nothing is pre-sealed; a courier carries named surplus that
  becomes takeable at a flag only on arrival. A move between flags is honoured
  only when **both** flags list the other in `reassign_to`
  (`Reassignment`/`rejected_reassignments`), and only once the bag arrives.

Late coverage is **additive**: a group that already holds a ticket is passed as
`frozen_tickets`, is never re-evaluated or revoked, and keeps its appointment.
Only still-open groups are replayed against the supplemented session.

### Rental plan (`rental_optimizer.py`)

The desk turns **real stall appointments** (plus pre-existing reservations) into
a plan honouring: per-person wait tolerance, same-stall FCFS (cells never
decrease along a stall's queue), one person per machine per cell, customers never
switch stalls, per-machine whole-shift price, per-machine **allowed first
stalls** (首投点), and stall-to-stall move cost. It returns the **minimum total
whole-shift rent**, the named per-cell machine dispatch, and the named person
called. Incremental mode (`locked` services + `paid_machines`) serves already-
called people first and, when the remainder can't finish, reports the
`earliest_unserviceable` ticket. The search is exact inside the brief's envelope
(≤6 groups, ≤8 cells, ≤3 stalls, ≤4 machines).

The brief's worked numbers are pinned in `tests/test_market_flow.py`: the
ordinary first group (rent 1 with one cell of wait, rent 2 for same-cell
service, rent 0 when the partition correction hides the flag); the near/far red
flags (near=3, far=2, no 1+2 combining, different stalls); and the Jia/Yi
en-route example under both protocols, where delaying the courier one cell
raises the minimum rent from 1 to 2.

## Scale / cost (20 sections, 500 facilities, 200 jobs)

Measured by `bench_scale.py`:

* **Point reverse lookup** — per-section scan, ~39 µs/query at 20 sections
  (~11 % ambiguous, ~88 % unresolvable in the stress route). A per-revision
  label-range index would make this `O(log n + k)` for the k duplicate
  candidates. Duplicate labels mean the result is a *set*, so no index can
  legitimately collapse it to one value.
* **Batch conversion** — ~0.18 ms/job: convert spans `O(#spans)`, merge
  `O(spans log spans)`, facility scan `O(#assets)`. 500 assets × 200 jobs =
  100k point checks; a physical-position index would reduce the facility
  side to an interval query per job.
* **Revision invalidation** — building the new revision is ~0.05 ms (copy
  the section rows). Marking old reports stale is metadata-only. The real
  work is re-resolving jobs that must be republished, `O(jobs × (spans +
  assets))`; in the stress route moving one section's base pushed 14/200
  spans outside the moved section's label range, and those jobs are
  correctly rejected rather than remapped.

## Explicit non-goals

No path optimisation, no measurement adjustment/least-squares, no inference
of missing positions. Nothing beyond the one explicit base correction is
changed.
