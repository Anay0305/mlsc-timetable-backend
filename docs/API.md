# MLSC Timetable — Backend API Reference

Reference contract for the other team. **Payload shapes here are normative** —
the frontend (and the bundled `public/fallback/` snapshot) depends on them
unchanged.

- Base URL: configurable per deployment; frontend reads `VITE_BACKEND_URL`.
- Content type: `application/json` (request and response), unless noted.
- All timestamps are ISO-8601 UTC.
- All error responses are shaped `{"detail": {"error": "<message>", "code": "<stable_code>", ...}}`.
  Treat `code` as the stable machine-readable handle; `error` is human-readable only.
- Companion machine-readable spec: [openapi.json](openapi.json) (FastAPI auto-generated).

## Auth model

| Surface | Header(s) | Notes |
| --- | --- | --- |
| Public reads (`/batch`, `/current`, `/timetable/{batch}`, `/contributors`, `/baselines*`) | none | Anonymous. |
| Per-user (`/me/*`) | `X-User-Id: <opaque-id>` | Opaque client-minted id, 4–64 chars, `[A-Za-z0-9_-]`. Server upserts a `UserDoc` row keyed on this. Missing/invalid → 400. |
| Public submit (`POST /change-requests`) | `X-User-Id` recommended | Used for the rate-limit key (falls back to client IP). |
| Admin (`/admin/*`) | `Authorization: Bearer <ADMIN_TOKEN>` | Single shared token from env. |

## Stable error codes

Emitted in `detail.code`. Clients should branch on these — never on the human text.

| Code | Usual status | Meaning |
| --- | --- | --- |
| `batch_not_found` | 404 | Timetable for batch does not exist. |
| `data_missing` | 503 | Required dataset (e.g. semester label) not seeded yet. |
| `rate_limited` | 429 | slowapi limiter tripped. |
| `quota_user` / `quota_batch` / `quota_global` | 429 | Storage-layer queue caps for change requests (20 / 100 / 1000 pending). |
| `duplicate` | 409 | Identical pending change request already exists. |
| `not_pending` | 409 | Approve/reject called on non-pending request. |
| `scope_requires_lecture` | 422 | `scope=class` only allowed when `entry.type == "Lecture"`. |
| `bad_kind` / `bad_scope` / `bad_day` / `bad_slot` / `missing_entry` / `bad_batch` | 400/422 | Body validation. |
| `no_batch` | 400 | `/me/*` called without a batch and user has no `default_batch`. |
| `invalid_payload` / `invalid_file` / `invalid_baseline` / `invalid_username` / `invalid_key` | 400 | Admin write validation. |
| `not_found` | 404 | Admin delete on a missing row. |

---

## Core domain objects

### `ClassEntry`

The canonical per-class shape. Returned inside `timetable.classes[]`, accepted in
`/me/overrides` and `/change-requests` bodies.

```json
{
  "day": "Monday",
  "start_time": "09:40",
  "end_time": "10:30",
  "subject": "Physics",
  "code": "UPH013P",
  "type": "Practical",
  "room": "G312",
  "options": []
}
```

Field rules:

- `day` — Title-case English weekday (`"Monday"`..`"Saturday"`).
- `start_time` / `end_time` — `"HH:MM"` 24-hour, leading zero. 50-minute slots
  on the canonical grid: `08:00, 08:50, 09:40, 10:30, 11:20, 12:10, 13:00,
  13:50, 14:40, 15:30, 16:20, 17:10` (and `18:00, 18:50` available in source).
- `subject`, `code`, `room` — optional strings.
- `type` — one of `"Lecture"`, `"Tutorial"`, `"Practical"`, or `"Unknown"`.
- `options` — list of elective alternatives (same shape, with `place`/`teacher`
  optional). Empty list for non-elective cells.

### `OverrideEntry`

```json
{ "kind": "edit", "entry": { ... ClassEntry ... } }
```

- `kind` ∈ `{"elective_pick", "edit", "delete", "add"}`.
- `entry` required for everything **except** `delete`.

### `Semester`

```json
{ "label": "EVEN 25-26" }
```

The leading word (`EVEN` / `ODD`) drives the baseline-prefix (`E` / `O`).

---

## Public endpoints

### `GET /healthz`
Liveness probe. Returns `{"ok": true}`.

### `GET /batch`
List all known batch codes.

```json
["1A11", "1A12", "1A13", ...]
```

### `GET /current`
Current semester label.

```json
{ "label": "EVEN 25-26" }
```

- 503 `data_missing` if not seeded.

### `GET /timetable/{batch}`
Canonical timetable for one batch.

```json
{
  "batch": "1A11",
  "semester": { "label": "EVEN 25-26" },
  "classes": [ { ...ClassEntry... }, ... ]
}
```

- 404 `batch_not_found` with `detail.batch` echoing the requested code.

### `GET /contributors`
Enriched contributor list (avatar fetched live from GitHub, cached 1 h).

```json
[
  {
    "id": 123456,
    "login": "octocat",
    "avatar_url": "https://avatars.githubusercontent.com/u/123456?v=4",
    "html_url": "https://github.com/octocat",
    "name": "The Octocat"
  }
]
```

Entries that GitHub 404s on are silently omitted. Order matches DB insertion.

### `GET /baselines`
List of expected per-type class counts per stream group. Used by the
`doctor` consistency report.

```json
[
  {
    "key": "E1A",
    "semester_prefix": "E",
    "group": "1A",
    "counts": { "Lecture": 12, "Tutorial": 4, "Practical": 3 },
    "updated_at": "2026-01-15T10:00:00Z"
  }
]
```

### `GET /baselines/{key}`
Single baseline (404 `not_found` if absent).

---

## Per-user endpoints (`X-User-Id` required)

### `GET /me`
Upserts `last_seen_at` and returns the public profile.

```json
{ "user_id": "abc-123", "display_name": null, "default_batch": "1A11" }
```

### `POST /me/batch`
Persist user's default batch.

Body: `{ "batch": "1A11" }` → response same as `GET /me`.

### `GET /me/timetable?batch=1A11`
Canonical timetable merged with the user's `OverrideDoc`.

```json
{
  "batch": "1A11",
  "semester": { "label": "EVEN 25-26" },
  "classes": [ ... ],
  "overrides_applied": 3
}
```

- `batch` query param is optional if the user has a `default_batch`; 400
  `no_batch` otherwise.
- Merge rules: `delete` removes the slot; `edit` / `elective_pick` replaces it;
  `add` appends a new slot.

### `GET /me/overrides?batch=1A11`
Full override map.

```json
{
  "batch": "1A11",
  "entries": {
    "Monday|09:40": { "kind": "edit", "entry": { ...ClassEntry... } },
    "Tuesday|13:00": { "kind": "delete", "entry": null }
  }
}
```

Key format: `"<Day>|<HH:MM>"` (single `|`).

### `PUT /me/overrides/{day}/{slot}?batch=1A11`
Upsert one override.

Body:
```json
{ "kind": "edit", "entry": { ...ClassEntry... } }
```

Response:
```json
{ "key": "Monday|09:40", "override": { "kind": "edit", "entry": {...} } }
```

### `DELETE /me/overrides/{day}/{slot}?batch=1A11`
Returns `{ "deleted": true|false, "key": "..." }`.

---

## Crowd-sourced change requests

### `POST /change-requests`  *(public, rate-limited)*

Rate limit (slowapi, key = `uid:<X-User-Id>` if header set, else `ip:<remote>`):
`5/minute; 30/hour; 100/day`.

Storage-layer caps (return 429 `quota_*`): 20 pending per requester, 100 per
batch, 1000 globally. Identical pending row → 409 `duplicate`.

Body:
```json
{
  "requester_batch": "1A11",
  "scope": "batch",            // or "class"
  "kind": "edit",              // "add" | "edit" | "delete"
  "day": "Monday",
  "start_time": "09:40",
  "entry": { ...ClassEntry... } // omit only for kind == "delete"
}
```

- `scope = "class"` is only allowed when `entry.type == "Lecture"` (422
  `scope_requires_lecture`). Class scope targets every batch sharing the first
  3 chars of `requester_batch` (e.g. `1B11/1B12/1B13/...`).
- Response (201): the stored `ChangeRequestDoc` snapshot, including the
  generated `id`.

---

## Admin endpoints

See [ADMIN_PANEL.md](ADMIN_PANEL.md) for feature-level docs. Every route
below requires `Authorization: Bearer <ADMIN_TOKEN>`.

- `GET /admin/health` → `{"ok": true, "scope": "admin"}`
- `PUT /admin/timetable/{batch}` → replace canonical timetable
- `PUT /admin/current` → set semester label
- `POST /admin/ingest` *(multipart)* → parse uploaded `.xlsx/.xlsm`
- `POST /admin/baselines/{key}` / `DELETE /admin/baselines/{key}`
- `POST /admin/contributors` / `DELETE /admin/contributors/{username}`
- `GET /admin/change-requests?status=pending&limit=100`
- `POST /admin/change-requests/{id}/approve`
- `POST /admin/change-requests/{id}/reject`

---

## CORS

`CORS_ORIGINS` env (csv) controls allowed origins. Default in dev:
`http://localhost:5173`. Production deployments must set this explicitly.

## Rate limiting

Provided by [slowapi](https://github.com/laurentS/slowapi). Public
`POST /change-requests` carries `5/minute;30/hour;100/day`. Limiter key is
`uid:<X-User-Id>` if the header is present, else `ip:<request.client.host>`.
A 429 response is shaped:

```json
{ "detail": { "error": "rate limit exceeded", "code": "rate_limited" } }
```

## Compatibility contract for the other team

Any breaking change to the **request or response shape** of any endpoint
documented here will break the live frontend (`mlsc-timetable/src/lib/*`) and
the bundled fallback in `mlsc-timetable/public/fallback/`. Specifically:

1. `GET /timetable/{batch}` response — keep `batch`, `semester.label`,
   `classes[].{day, start_time, end_time, subject, code, type, room, options}`.
2. `GET /batch` — keep flat `list[str]` of batch codes.
3. `GET /current` — keep `{label}`.
4. `GET /me/timetable` — keep same shape as `GET /timetable/{batch}` plus
   `overrides_applied: int`.
5. `X-User-Id` header semantics — opaque client-minted id, no server validation
   beyond the regex; do not start returning 401/403 for unknown ids.
6. Error envelope — keep `{detail: {error, code}}`.

If a new field is needed, *add* it; do not rename or remove existing ones
without coordination.
