# Room & teacher schedules, availability, and improvement planning

Three related features built on one cross-batch index:

1. **Location-wise timetables** — what runs in a room, all week.
2. **Teacher-wise timetables** — what a teacher takes, all week.
3. **Availability** — who/what is free at a given time.
4. **Improvement planning** — which junior batch a student repeating an
   earlier course can actually sit with.

## Why an index exists

Stored timetables are batch-shaped. One physical lecture is copied into every
attending batch's document, so `UCS503L` in `LT102` on Monday 08:00 appears in
twenty documents. Reading a room straight from those documents shows the same
class twenty times.

`server/schedule_index.py` folds duplicates into one `Occupancy` carrying the
attending batches. On current data that turns 31,854 stored class rows into
7,562 real occupancies across 300 rooms and 629 teachers, built in ~400 ms.

Two normalizations happen during the build.

**Elective cells are exploded.** An elective has no room of its own — each
option does — so a four-option elective books four rooms in that slot even
though a student attends one of them.

**Room strings are canonicalized** (`server/room_names.py`). The workbook
spells one room several ways:

| Written | Canonical | Note |
|---|---|---|
| `L307`, `AI(L307)` | `L307` | the lab name is a label, not a room |
| `GC-2(L107` | `L107` | unclosed bracket |
| `HIGH VOLTAGE-C101` | `C101` | room embedded in a name |
| `B204/F314` | `B204` + `F314` | genuinely two rooms — both are booked |
| `CBCL/G114` | `G114` | a lab name beside its room, not two rooms |
| `Not Given`, `?` | *(none)* | no room |
| `BAJAJ LAB` | `BAJAJ LAB` | a real room that simply has no number |

Without this, `AI(L307)` and `L307` index separately and availability reports
one free while the other is occupied — exactly the question the view exists to
answer. Normalization merges 139 duplicate spellings.

**A class needs only one thing to identify it.** 95 rows across 45 batches carry
a course code but neither a room nor a teacher. They cannot appear in a room or
teacher view, and those buckets skip them, but they are real commitments — so
they are still indexed by course and batch. Dropping them hid both the course
from improvement planning and the clash it causes. Only a row with no room, no
teacher *and* no code is discarded.

**Term labels come from the current semester, not the document.** A batch
missed by the latest ingest keeps its old label; 21 batches currently still say
`EVEN 25-26`. Deriving semester parity from that would place them in even
semesters in the middle of an odd term. The index uses the current term label
and reports the mismatched batches as `stale_term_batches`.

## Caching

The index is cached in-process for `SCHEDULE_INDEX_TTL_SECONDS` (default 300)
and invalidated immediately by `write_timetable` / `delete_timetable`, which are
the only things that can change it. `POST /admin/schedule/rebuild-index` forces
a rebuild.

The cache is per process. With more than one API worker each holds its own copy,
which is fine — it is derived data, and the TTL bounds staleness.

## Endpoints

### Rooms and teachers

| Route | Notes |
|---|---|
| `GET /schedule/meta` | slot grid, teaching days, directory sizes |
| `GET /schedule/rooms` `?q=` | room directory |
| `GET /schedule/rooms/{name}` | one room's week |
| `GET /schedule/rooms/{name}/free?day=` | contiguous gaps that day |
| `GET /schedule/availability/room?day=&at=` | free vs busy at a time |
| `GET /schedule/teachers…` | same four shapes, for teachers |

`at=HH:MM` probes an instant; `start=`/`end=` probe a window. `day` accepts
`mon` or `Monday`.

### Teacher access is gated

Teacher codes are hidden per batch by `BatchDoc.teacher_codes_visible`, and
`redact_teacher_codes` strips them from public timetables. A public teacher
directory would bypass that, so `/schedule/teachers*` **requires admin
credentials by default**. Set `TEACHER_SCHEDULE_ACCESS=public` to open it to
everyone. Room routes are always public and edge-cacheable.

### Improvement planning

| Route | Notes |
|---|---|
| `GET /improvement/courses?batch=` | what that student may repeat |
| `POST /improvement/plan` | `{batch, codes[]}` → ranked options + plans |

`/plan` is `private, no-store`: when the caller presents a Clerk token their
saved elective picks are folded in first, which narrows their committed slots
and turns rejected offerings back into viable ones.

## The two rules

**Which semesters are reachable.** Semesters run in parity lockstep — in an odd
term every batch is in an odd semester — so a 5th-semester student can never sit
a 4th-semester course; it is not running. Semesters 1 and 2 are pooled by
default (`IMPROVEMENT_POOL_FIRST_YEAR_SEMESTERS=1`) because the first-year pools
swap courses between the two halves of the year.

**What clashes are tolerable.** A clash is graded by the **stronger of the two
classes involved**. If the student's own lab runs against the improvement
lecture, that is a *practical* clash — the lab cannot be skipped, so the lecture
would be lost every week. Practicals default to zero tolerance; lectures and
tutorials default to one each. All three are configurable.

An **unresolved** elective in the student's own timetable blocks its slot at the
worst severity among its options and is flagged `uncertain`, so the UI can say
"this depends on which elective you pick" instead of silently rejecting.

Once the choice is made the slot stops being uncertain. The entry keeps its
`options` list after the pick, so the elective state has to be read from
`electiveChoice`, not from the presence of options — otherwise a student who
picked the lecture is still charged for the practical they are not attending,
and the picks are merged in precisely to stop that. A slot marked
`electiveDismissed` (the group was resolved in a different period) is free and
blocks nothing.

**A clash is counted per class, not per period.** Timetables are stored one
period per row, so a two-hour lab is two rows; 99% of practicals and 407
lectures in the current data run for more than one period. Pairing rows would
make a single overlapping lab score two clashes — four against another
two-period lab — which silently breaks a limit that was written to allow one.
Consecutive rows of the same course, type, room and teacher are folded into the
one session the student attends before anything is counted or displayed. This
is also what makes `IMPROVEMENT_MAX_PRACTICAL_CLASHES=1` do what it says.

## Output shaping

A course is offered by many parallel batches that sit in the same lecture.
Those are interchangeable, so options are grouped by what the student would
actually attend: `UTA016`'s 156 candidate batches collapse to 40 distinct
schedules, each listing its interchangeable `batches`.

The grouping key includes **room and teacher**. Batches genuinely sharing one
lecture fold into a single occupancy upstream and still group together, but two
batches taught the same course at the same hour in different rooms are parallel
sections, not one class — collapsing them would print one section's room beside
the other section's batch code and send the student to the wrong place.

Course codes normalize to their base form, so one course covers its L/T/P
components — repeating `UCS301` means attending its lecture, tutorial and
practical. `resolve_code` prefers an exact index hit before normalizing,
because `base_course_code` is not idempotent for non-standard codes
(`BEST` would otherwise become `BES` on a second pass).

When several courses are requested, `plans` proposes combined timetables: each
course is checked against the student's timetable *plus* the offerings already
chosen, so two improvement courses can never be scheduled on top of each other.
The clash budget applies per course.

The search is exponential in the number of courses, so it is bounded by the
combinations it examines (20,000) rather than by the plans it keeps. Everything
it reaches is ranked before `IMPROVEMENT_MAX_PLAN_OPTIONS` are returned, so the
plans are the best found rather than the first found — stopping at the first N
complete plans would return N variations on one course's first option and
present that as a ranking. `plans_truncated` says whether anything was left
unranked, so the UI can admit a better combination may exist.
