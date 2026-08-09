# Shared lab slots misread as electives

**Status:** known defect, not yet fixed. Documented 9 August 2026 against the
live ODD 26-27 data.

## Symptom

On batch `2B11` the timetable shows an elective picker offering two options with
the *same subject name*. Picking one leaves the student with a lab and no
lectures for it; picking the other lines up with lectures they already have.

## What it actually is

It is not an elective. The cell holds **two different batches' labs that run in
the same slot**, printed into one spreadsheet cell. The parser sees two subject
codes in one cell and classifies it as a two-option elective group.

`2B11`, Tuesday 14:40 and 15:30:

| Option | Room | Teacher | Whose lab it is |
|---|---|---|---|
| `UCC305P` Solid Mechanics | LAB (W123) | SM | `2U11` |
| `UCE312P` Solid Mechanics | *(none)* | SHR | `2B11` |

Different rooms and different teachers, one subject name. Which option belongs
to which batch is not a choice — it is determined by the lectures each batch
already has:

- `2B11` has `UCE312L` ×3 and `UCE312T` → its lab is `UCE312P`.
- `UCC305` lectures and tutorials exist in exactly one batch, `2U11` → that is
  whose lab `UCC305P` is.

The sibling batches confirm it: **`2B12` and `2B13` carry `UCE310P`/`UCE312P` as
ordinary practicals**, not electives. Only `2B11` had them merged into a shared
cell, because only there did the workbook print both batches' labs together.

The same pattern appears Friday 11:20/12:10 with `UCC306P` / `UCE310P`.

## Scale

Measured against production on 9 August 2026:

| | |
|---|---|
| Elective cells with ≥2 coded options | 1,563 |
| Cells where every option resolves to the **same** subject name under different codes | **8** |
| Batches affected | **2** — `2U11` (4 cells), `2B11` (4 cells) |
| Of those, cells where exactly one option matches a course the batch has lectures for | **8 — all of them** |

Every affected cell is deterministically resolvable. There is currently no case
needing a human decision.

> A first pass at this measurement reported 18 cells across 7 batches. That was
> wrong: the filter discarded options whose name could not be resolved, so a
> cell holding one named and one unnamed option looked like "all options are the
> same subject". Requiring *every* option to resolve before comparing brings it
> down to 8 cells in 2 batches. The 3S11–3S15 cells it wrongly flagged are an
> instance of the separate naming gap below.

## Proposed fix

A rule at projection time (`server/curriculum_projection.py`), where the live
Library overlay already rewrites elective cells:

1. If **every** option in a cell resolves to a name, and all those names are
   identical, while the base codes differ → the cell is a shared slot, not a
   choice.
2. Collapse it to the single option whose base code appears in that batch's own
   lectures or tutorials, and render it as a normal practical.
3. If zero or more than one option matches, leave the cell as-is and raise a new
   finding — suggested `SHARED_SLOT_UNRESOLVED` — so it lands in the Fix queue
   rather than silently rendering a choice that is not a choice.

Step 3 has no live instances today but is the safe default for future workbooks.

Projection is the right layer rather than the parser: the stored observation
should keep saying what the workbook said, and the meaning should be applied at
read time — the same rule the Library overlay already follows.

## Why it is not fixed yet

It affects 2 of 503 batches. It was found by inspection, not by the error
detector, and the fix touches the elective projection that every timetable read
goes through — so it wants its own change and its own tests rather than being
folded into unrelated work.

## Detection is not automatic

This produced **zero findings**. No error type covers "an elective cell that is
not an elective", so it never reached the Fix queue and is not among the 674
findings from the current ingest. Any claim that every problem is visible in the
admin panel is true only of what the detector looks for.

Note also that `curriculum_library` is **empty in production**, so
`project_curriculum_payload` returns `available: False` and the Library overlay
is entirely inert. Elective cells today are exactly what the parser produced.

## Re-running the measurement

```python
# base_subject_code() normalises UCE312P -> UCE312
for doc in db.timetables.find({}, {"code": 1, "classes": 1}):
    own = {base_subject_code(k["code"]) for k in doc.get("classes") or []
           if k.get("code") and k.get("type") in ("Lecture", "Tutorial")}
    for k in doc.get("classes") or []:
        opts = [o for o in (k.get("options") or []) if o.get("subject_code")]
        if len(opts) < 2:
            continue
        names = [resolve_name(o) for o in opts]          # stored name or catalog
        bases = {base_subject_code(o["subject_code"]) for o in opts}
        if all(names) and len(set(names)) == 1 and len(bases) > 1:
            match = [o["subject_code"] for o in opts
                     if base_subject_code(o["subject_code"]) in own]
            ...   # len(match) == 1 -> collapse to match[0]; else raise a finding
```

## A separate, larger issue this measurement surfaced

**320 elective cells across 87 batches contain at least one option whose subject
name cannot be resolved.** That is the naming gap, not this defect — those cells
are real electives, they simply show a bare course code for some options. It
shrinks whenever the catalog gains the missing codes; see
`scripts/find_subject_titles.py`.
