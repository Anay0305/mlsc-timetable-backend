# Admin Panel — Backend Feature Reference

Every endpoint below is mounted under `/admin` and protected by a single
`Authorization: Bearer <ADMIN_TOKEN>` check (`server.auth.require_admin`,
constant-time compare via `hmac.compare_digest`). There is no per-user admin
role yet — anyone with the token is fully privileged.

`ADMIN_TOKEN` comes from the process env. If unset, the dependency raises 503
`admin_token_unset` for every admin request.

For raw OpenAPI: [openapi.json](openapi.json). For payload shapes shared with
public endpoints: [API.md](API.md).

---

## 1. Health probe

`GET /admin/health` → `{"ok": true, "scope": "admin"}`

Use this to confirm the admin token is wired correctly. Returns 401
`unauthorized` if the bearer is wrong/missing.

---

## 2. Spreadsheet ingest (multipart upload)

`POST /admin/ingest` — primary onboarding flow each semester.

Form fields:

| Field | Type | Notes |
| --- | --- | --- |
| `semester` | text | e.g. `"EVEN 25-26"`. Drives the doctor's `E`/`O` baseline prefix. |
| `sheet` | text | `"all"` (default), `"active"`, `"@<n>"` (1-indexed), an exact sheet title, or a glob. |
| `file` | file | The `.xlsx` / `.xlsm` workbook. |

Response:
```json
{
  "ok": true,
  "semester": "EVEN 25-26",
  "sheet": "all",
  "batches": 459,
  "classes": 12537,
  "sheets_used": ["Sheet1", "Sheet2"],
  "multi_sheet_batches": ["1B14", "2A03"],
  "doctor": { ...build_doctor_report output... }
}
```

Pipeline behaviour (`server.ingest.parse_workbook`):

1. Iterates all visible worksheets (skips hidden).
2. Calls the parser library (`timetable_parser/`) per sheet → `ClassBlock[]`.
3. Dedup-merges blocks when the same batch appears in multiple sheets via
   `_block_dedup_key(day, start_slot, periods, code, type, block_kind)`.
4. Serializes to API shape via `class_blocks_to_api(blocks, semester)`.
5. Upserts `SemesterDoc`, `BatchDoc`s, `TimetableDoc`s.
6. Runs `build_doctor_report` against the current `semester_prefix` baselines
   so the response includes a consistency summary.

When `JSON_MIRROR=1` is set, parallel snapshots are written to `DATA_DIR`
(`batch.json`, `current.json`, `timetable/<batch>.json`) using atomic
`os.replace`. When `GIT_AUTO_COMMIT=1` is set, those mirrors are
auto-committed.

Failures: 400 `invalid_file` (wrong extension or empty upload); parser/validation
errors are passed through as 422 with `code=invalid_payload`.

> CLI equivalent: `mlsc-timetable build <xlsx> --semester "..." --sheet all
> [--mirror-json --out data/] [--git-commit]` — same code path, no HTTP.

---

## 3. Manual timetable editing

`PUT /admin/timetable/{batch}` — replace one batch's canonical timetable.

Body: full `{ batch, semester, classes }` document (same shape as the public
`GET /timetable/{batch}` response). Server validates that `classes` is a list
and every entry has `day`, `start_time`, `end_time`, `type`.

On success: writes via `storage.write_timetable`, returns
`{"ok": true, "batch": "<code>"}`, and (if `GIT_AUTO_COMMIT=1`) auto-commits
the JSON mirror.

Use this to hot-patch a single batch without re-running ingest.

---

## 4. Semester label

`PUT /admin/current` — set the global semester label shown in the landing card.

Body: `{ "label": "ODD 26-27" }` → `{"ok": true, "label": "ODD 26-27"}`.
400 `invalid_payload` if `label` is missing or not a string.

---

## 5. Baselines (doctor expectations)

The doctor compares observed per-type class counts against expected baselines
per stream group (`{semester_prefix}{YEAR}{ALPHA}`, e.g. `E1A`).

- `POST /admin/baselines/{key}` body `{"counts": {"Lecture": 12, "Tutorial": 4,
  "Practical": 3}}` → upsert.
- `DELETE /admin/baselines/{key}` → 200 on success, 404 `not_found` if absent.

`key` is normalized to uppercase. Validation: `counts` must be a `dict[str,int]`
with non-negative integers (400 `invalid_baseline` otherwise).

Use these to lock in a known-good shape *before* re-ingesting a fresh
spreadsheet — the post-ingest doctor report will then surface any batch that
drifted.

---

## 6. Contributor roster

The DB stores only GitHub **usernames**; the public `GET /contributors` endpoint
enriches each one live from `https://api.github.com/users/<u>` (cache TTL
`CONTRIBUTORS_CACHE_TTL`, default 3600 s; `GITHUB_TOKEN` raises the rate limit
from 60 to 5000 req/h).

- `POST /admin/contributors` body `{"username": "octocat", "display_name": "The
  Octocat"}` — `display_name` optional. Username is `strip().lstrip("@")`-ed.
- `DELETE /admin/contributors/{username}` → 200 on success, 404 if absent.

No avatars are stored. To "refresh" an avatar after a user changes their
GitHub profile picture, simply wait for the in-process cache to expire (or
restart the server).

---

## 7. Change-request moderation

Crowd-sourced edits land in `ChangeRequestDoc` with `status="pending"`. The
admin sub-router (`/admin/change-requests`) is the only way to triage them.

### `GET /admin/change-requests`

Query params:
- `status` — `pending` | `approved` | `rejected` (default: all).
- `limit` — int 1..500 (default 100).

Response:
```json
{
  "items": [
    {
      "id": "65b...",
      "requester_id": "abc-123",
      "requester_batch": "1B11",
      "semester": "EVEN 25-26",
      "scope": "batch",
      "kind": "edit",
      "day": "Monday",
      "start_time": "09:40",
      "entry": { ...ClassEntry... },
      "status": "pending",
      "created_at": "2026-06-26T10:11:12Z",
      "decided_at": null,
      "decision_note": null
    }
  ],
  "count": 1
}
```

### `POST /admin/change-requests/{id}/approve`

Body (optional): `{"note": "looks good"}` (≤500 chars).

Effect: rewrites the canonical `TimetableDoc.classes` for every batch in scope.
For `scope=batch`, that's just `requester_batch`. For `scope=class`, it's
every batch whose code starts with the first 3 chars of `requester_batch`
(e.g. an approval on `1B11` rewrites `1B11`, `1B12`, `1B13`, ...).

Failure codes:
- 404 `not_found` — id does not exist.
- 409 `not_pending` — already approved/rejected.
- 409 `empty_scope` / `empty_targets` — class-scope expanded to no batches.

### `POST /admin/change-requests/{id}/reject`

Same body as approve. Marks status `rejected`, sets `decided_at` and
`decision_note`. Same 404/409 failure modes.

### Storage-layer guards (visible to admins)

The public submit path enforces these *before* a row is written:
- `MAX_PENDING_PER_REQUESTER = 20` → 429 `quota_user`.
- `MAX_PENDING_PER_BATCH = 100` → 429 `quota_batch`.
- `MAX_PENDING_TOTAL = 1000` → 429 `quota_global`.
- Duplicate guard on `(batch, scope, slot, kind)` → 409 `duplicate`.
- `scope=class` requires `entry.type == "Lecture"` → 422 `scope_requires_lecture`.

If the global cap fills up, approving or rejecting pending rows is the only
way to admit new ones.

---

## 8. Operational notes

- **Auth**: rotate `ADMIN_TOKEN` by restarting the process with a new env. No
  invalidation list; the token is the only credential.
- **Audit trail**: there isn't a dedicated audit log. Approvals/rejections
  store `decided_at` + `decision_note` on the change-request row itself.
  `PUT /admin/timetable/{batch}` and `PUT /admin/current` produce a git commit
  on the JSON mirror when `GIT_AUTO_COMMIT=1`.
- **Doctor**: every successful `POST /admin/ingest` runs the doctor and the
  response carries the summary — no separate run-doctor endpoint.
- **Backups**: turn on `JSON_MIRROR=1` so each write produces a JSON file in
  `DATA_DIR/timetable/`. The frontend's `public/fallback/` snapshot is built
  by copying this directory.
