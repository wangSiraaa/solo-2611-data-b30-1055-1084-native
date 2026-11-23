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

# Static display survey: leveling profile + two-wheel carts

The chainage catalog above answers *where* a label and an inspection really
are. Two further modules turn the published coverage plus the leveling crew's
readings into a road profile and then into static sign/no-sign verdicts for
the scale-model display carts. All arithmetic is exact `Fraction`; only the
static display is checked, never rolling/driving.

## `leveling_profile.py` — setups to a road profile

Each `LevelSetup` is one instrument standing with its **own arbitrary zero**.
Only reading *differences* are facts, and the task uses height-signed
readings, so within one setup:

```
elev(b) - elev(a) = r_b - r_a
```

The setup zero cancels; a raw reading is never an elevation. One
`Benchmark` stake fixes the absolute datum. The network is a
difference-constraint graph solved by BFS:

* the benchmark's component gets absolute elevations;
* a component **disconnected from the benchmark keeps relative elevations
  only** — its absolute level is reported unknown (`None`), never assumed
  zero;
* a loop whose two paths disagree is a `Misclosure`. Both records are named
  and `GroundProfile` refuses to publish until it is resolved (no
  single-valued height exists).

Walked coverage is a separate fact layer (a merged interval union reused
from `chainage_catalog`). `GroundProfile.ground_at(x)` interpolates linearly
**only between adjacent measured stakes inside one walked interval**; an
un-walked gap returns `None` and is never bridged by interpolation.

Worked example (the task's headline): setups read (0,1)=(4,5) and (1,2)=(9,8),
stake 0 benchmarked at 0 → elevations `0, 1, 0`. Changing only the two middle
readings to 6 and 10 gives `0, 2, 0`.

## `display_carts.py` — plate clearance, stands, bridges, scheduling

A `Cart` rests on two point wheels; its bottom plate is the contact-point
chord plus a fixed `plate_rise`. `check_cart` returns three distinct states:

| status       | meaning                                                            |
|--------------|-------------------------------------------------------------------|
| `signable`   | every measured point over the span has clearance `>= 0` (0 = touch) |
| `scrapes`    | a *measured* point has negative clearance                          |
| `unmeasured` | the span crosses an un-walked gap; undecidable, **not** a scrape   |

Because both the plate chord and each ground segment are linear, the minimum
clearance on a segment occurs at a measured stake, so proven stakes suffice
and no unmeasured height is invented. The result names the tightest binding
segment.

Remediation devices are modelled explicitly and are not interchangeable:

* `required_stand_height` / `StandAssignment` — a set of jacks **locked to
  the two named wheels of one cart**, lifting both to one common height. A
  stand stays under its cart for the whole show; with `max_stands=1` two
  scraping mandatory carts cannot both be saved (the set cannot be moved).
* `verify_bridge` / `minimum_clearance_bridge` — a straight deck between two
  measured seats that must clear every measured point in the span. A seat on
  an unmeasured point or an un-walked gap inside the span makes the bridge
  baseless and unsigned; weight and roadside footprint are enforced as
  scheduling limits.

`plan_exhibition` exhaustively enumerates mandatory placements and optional
candidate placements (bounded by the 12-stake / 8-setup / 5-cart static
model), maximises carts kept (= minimises cancellations), forbids internal
road overlap while allowing shared endpoints, and reports each kept cart's
named position plus the overall minimum clearance and the segment it comes
from. A mandatory cart with no signable placement makes the whole exhibition
infeasible with a stated reason, kept separate from a confirmed scrape.

## Running the display modules

```bash
python3 solution/run_display_demo.py
python3 -m unittest discover -s solution/tests -v
```

`run_display_demo.py` prints every task scenario end to end: the 0,1,0 and
0,2,0 profiles, the half-rise scrape, the named misclosure, the disconnected
component, the precise un-walked gap `[1,2)`, the repeated-label ambiguity,
and the crest with one-stand shortfall / two-stand / one-bridge outcomes
including the bridge losing its basis when a seat becomes unmeasured.

