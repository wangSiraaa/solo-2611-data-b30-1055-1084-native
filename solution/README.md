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
python3 solution/run_demo.py                    # in-memory
python3 solution/run_demo.py --db route.db      # durable (resumable)
python3 -m unittest discover -s solution/tests -v
python3 solution/bench_scale.py                 # 20 sections / 500 assets / 200 jobs
```

`run_demo.py` reads `materials/730` operation by operation, applies the P2
base correction, and prints the real facility change (B in r1 → C in r2).
A file-backed DB records an op-log so re-running resumes (mutating ops are
skipped, read-only ops still render).

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

---

# Daytime shadow-puppet stage (`shadow_stage.py`, `scene_730.py`)

A separate subsystem that *uses* the physical route above: it never treats a
chainage label as a global coordinate. `scene_730.py` binds each performance
slot (影位) to a real rebuilt locator plaque (`asset`) on route `L` and
certifies it against the per-section inspection coverage.

## Slots, lights, and the citable board basis

* A **slot** sits on an integer puppet-layout grid (`grid_x`), separate from
  the route physical axis. It is `walked` only if the survey reached it.
* Per beat a **light** has a direction (`W`/`E`) and a positive rational
  slope. A **shade board** on the *up-light* side at grid distance `d` blocks
  the slot when `height > d * slope`; equality leaves light.
* Only the **nearest** up-light board matters — once a nearer board is too
  short the beam passes and no farther board is cited. `light_report()`
  returns each slot's status (`lit`/`dark`/`unresolved`) with the exact
  `height > d*slope` comparison for every dark slot.
* A slot on an **un-walked** leg, an **unmeasured** up-light board, or a
  locator outside the inspected coverage is `unresolved` — it can never be
  signed as lit **and never as a usable dark hiding slot**. This is why the
  round answer "甲格有光" from the route alone is *not* enough to sign any
  follow-up: the certification must reach the source side.

## Grid shapes and occlusion

Each puppet carries a grid shape (≤ 3×3) with one **face** cell. A lit slot
projects its shape onto the screen; the front row (OC1) covers the back row
(OC2) wherever the front puppet's shape includes the back puppet's face cell.
A dark slot projects nothing. `visible_faces()` returns the faces actually
seen, so swapping the front/back order can genuinely hide one face.

## Scheduling, rebinding, standby and flip rigs

`compile_show()` validates an **explicitly declared** beat block (no search)
and reports the **earliest beat it cannot join**:

* One puppet per master, one master per slot per beat, one appearance per
  puppet per beat.
* A master may only present a role it has **learned**. Rebinding to a
  *different* puppet needs one empty beat; movement and turning obey the
  stage's declared beat costs.
* A **standby** prebinds a real puppet to a named substitute (who may lift it
  on time from a wing). While a standby holds, the home venue cannot borrow
  that substitute or puppet on the immediately following beat.
* A **flip rig** preloads two puppets on one master so the face changes
  inside a beat, at the cost of one extra measured, lit, occupiable slot; the
  old face must be kept hidden along the flip seam or it is caught as a leak.
* **Absence notices** are mandatory: if a notice says a role's bound master
  is absent this beat, using that master is rejected; the substitute must be
  among the on-hand masters; and a switch may not be acted on before the
  notice is actually known (no premature switching).
* `resume=` continues from an already-performed state (last performance,
  puppets on hand, beats done), so a shift handover starts from reality.

Stage limits (`check_limits`): ≤ 4 masters, ≤ 5 puppets, ≤ 6 beats,
≤ 4 slots, grid ≤ 3×3.

## Running

```bash
python3 solution/run_shadow_demo.py             # both worked shows
python3 -m unittest discover -s solution/tests -v
```

The demo prints: both-beat-lit handoff (board height 1); the same handoff
with the board raised to height 2 (beat 1 dark, rabbit can't show); the
single-master rebind failure; a front/back depth swap where a face is
covered; the standby substitute; and the flip-rig alternative with the
forbidden passing-face check.
