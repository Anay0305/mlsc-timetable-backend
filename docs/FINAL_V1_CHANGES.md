# `final-v1` vs `main` — Backend Changes

Summary of everything we shipped on the `final-v1` branch on top of `main`.
**Net**: +2 335 lines tracked in 23 files, plus two new untracked modules
(`server/rate_limit.py`, `server/routers/change_requests.py`, +155 lines).

This is the doc to hand to the team that owns the backend going forward —
along with [API.md](API.md), [ADMIN_PANEL.md](ADMIN_PANEL.md), and
[openapi.json](openapi.json).

---

## High-level themes

1. **Bootstrap of the FastAPI server.** `main` had only the spreadsheet parser
   library — `final-v1` adds the entire HTTP surface (`server/`).
2. **Mongo is the source of truth.** Beanie ODM over Motor. JSON files on disk
   are an opt-in *mirror*, not the canonical store.
3. **Per-user overrides** via opaque `X-User-Id` (no real auth yet) — users can
   edit their own grid server-side without touching the canonical timetable.
4. **Crowd-sourced change requests** + admin moderation pipeline, rate-limited
   on both the framework (slowapi) and storage layers.
5. **Contributor roster** stored as bare GitHub usernames, avatars fetched live
   from the GitHub REST API (1-hour in-process cache).
6. **Multi-sheet ingest** with cross-sheet dedup and a built-in doctor report.
7. **CLI** (`mlsc-timetable {build|migrate-json|doctor}`) wired to the same
   storage layer the HTTP API uses.

---

## New modules (created on `final-v1`)

### Server bootstrap & infra
| File | Purpose |
| --- | --- |
| `server/__init__.py` | Package marker. |
| `server/app.py` | `create_app()` factory: CORS, slowapi middleware, lifespan (`init_db` / `close_db`), exception handlers (`RateLimitExceeded` → 429 `rate_limited`; `storage.DataMissing` → 503 `data_missing`), `/healthz`, all routers wired in. |
| `server/config.py` | Env loader: `DATA_DIR`, `CORS_ORIGINS`, `ADMIN_TOKEN`, `GIT_AUTO_COMMIT`, `MONGODB_URL`, `MONGODB_DB`, `JSON_MIRROR`. |
| `server/auth.py` | `require_admin` (constant-time bearer compare); `require_user_id` (regex-validated `X-User-Id` header, no auto-mint). |
| `server/rate_limit.py` *(untracked)* | Single `slowapi.Limiter` instance, key = `uid:<X-User-Id>` if present, else `ip:<remote>`. |

### Persistence layer
| File | Purpose |
| --- | --- |
| `server/db/__init__.py` | Re-exports `init_db` / `close_db`. |
| `server/db/init.py` | Motor client + Beanie `init_beanie` over `ALL_DOCUMENTS`. Idempotent. |
| `server/db/models.py` | Beanie `Document`s: `SemesterDoc`, `BatchDoc`, `TimetableDoc`, `UserDoc`, `OverrideDoc`, `BaselineDoc`, `ContributorDoc`, `ChangeRequestDoc`. Plus shared `ClassEntry`, `OverrideEntry` (`Literal`-typed kinds), `ElectiveOption`, `TimetableSource`. |
| `server/storage.py` | Async dict-shaped facade over the ODM. Decouples HTTP contract from Beanie types. Exceptions: `BatchNotFound`, `DataMissing`, `ChangeRequestRefused(code=...)`. Helpers: `read_*` / `write_*` for batches/current/timetables, baseline CRUD, contributor CRUD, change-request lifecycle (`create_change_request`, `list_change_requests`, `approve_change_request`, `reject_change_request`), `maybe_git_commit`, `semester_prefix(label)`. Optional `JSON_MIRROR=1` mirror writes via atomic `os.replace`. |

### Routers
| File | Surface |
| --- | --- |
| `server/routers/batch.py` | `GET /batch` → `list[str]`. |
| `server/routers/current.py` | `GET /current` → `{label}`. |
| `server/routers/timetable.py` | `GET /timetable/{batch}` → full canonical payload, 404 `batch_not_found`. |
| `server/routers/baselines.py` | `GET /baselines`, `GET /baselines/{key}`. |
| `server/routers/contributors.py` | `GET /contributors` — enriches `ContributorDoc.username` with `{id, login, avatar_url, html_url, name}` from `https://api.github.com/users/<u>`, in-process cache `CONTRIBUTORS_CACHE_TTL` (default 3600 s), uses `GITHUB_TOKEN` if set. |
| `server/routers/me.py` | Per-user surface (`require_user_id`): `GET /me`, `POST /me/batch`, `GET /me/timetable`, `GET /me/overrides`, `PUT/DELETE /me/overrides/{day}/{slot}`. Merges `OverrideDoc` into the canonical timetable server-side. |
| `server/routers/change_requests.py` *(untracked)* | Public `POST /change-requests` (rate-limited `5/minute;30/hour;100/day`) + admin sub-router `/admin/change-requests` (`GET ?status=...&limit=...`, `POST /{id}/approve`, `POST /{id}/reject`). Class-scope only for `Lecture`. |
| `server/routers/admin.py` | All admin writes (token-gated): `GET /admin/health`, `PUT /admin/timetable/{batch}`, `PUT /admin/current`, `POST /admin/ingest` (multipart), `POST/DELETE /admin/baselines/{key}`, `POST/DELETE /admin/contributors[/{username}]`. |

### Ingest + doctor
| File | Purpose |
| --- | --- |
| `server/ingest.py` | `parse_workbook(xlsx_path, semester_label, *, sheet='all')` — iterates worksheets, dedup-merges via `_block_dedup_key`, calls `class_blocks_to_api`, writes `current`/`batch`/`timetable` collections, runs doctor against current-prefix baselines. Returns `{batches, classes, sheets_used, multi_sheet_batches, doctor}`. |
| `server/doctor.py` | `count_classes`, `build_doctor_report` (groups by `code[:2]`, compares to baseline or per-type mode), `doctor_report_from_dir`, CLI text `format_doctor_report`. |

### CLI + serializer
| File | Purpose |
| --- | --- |
| `timetable_parser/cli.py` | Console script `mlsc-timetable` exposing `build` (HTTP-equivalent ingest), `migrate-json` (one-shot legacy JSON → Mongo), `doctor`. All commands `await init_db()` first. |
| `timetable_parser/serializers/api.py` | `class_blocks_to_api(blocks, semester_label) -> {batch: payload}` — splits multi-period blocks per-slot, Title-case days/types, derives `room` via `is_place_like`. |

### Project / env
| File | Purpose |
| --- | --- |
| `pyproject.toml` | New deps: `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `python-multipart>=0.0.9`, `beanie>=1.27`, `motor>=3.5`, `httpx>=0.27`, `slowapi>=0.1.9`. Adds `mlsc-timetable = "timetable_parser.cli:main"` console script. Hatchling now builds **both** `timetable_parser` and `server` packages. |
| `.env.example` | Documents every env var the server reads (Mongo, CORS, admin token, mirroring, GitHub token, etc.). |
| `.gitignore` | Adds `.venv/`, `data/timetable/*.json` exclusions, etc. |

---

## Bug-fix / behaviour-change snapshot

These are the smaller, easy-to-miss decisions baked into `final-v1`:

- **No auto-minted user ids.** `require_user_id` rejects missing/invalid
  `X-User-Id` with a 400 — clients (including the frontend) own id generation.
- **`OverrideDoc` is one row per `(user_id, batch)`**, not per cell. `entries`
  is a dict keyed `"day|start_time"`. Avoids a Mongo document per edit.
- **`ChangeRequestDoc` keeps the entire `entry` snapshot** so approvals are
  idempotent even if the canonical timetable mutates in between.
- **Class-scope approval rewrites every batch sharing the 3-char prefix.**
  Done via `_resolve_target_batches`; if the prefix expands to zero rows the
  approval fails 409 `empty_targets` instead of silently no-op-ing.
- **`JSON_MIRROR=0` by default.** Production runs don't keep on-disk JSON;
  enable only when you want a portable backup or to feed the frontend's
  fallback snapshot.
- **Contributors never store avatars.** Live fetch + 1 h cache keeps the roster
  always-fresh without manual upkeep.
- **slowapi key is uid-first, ip-fallback.** Two users behind the same NAT
  don't share a quota if both present `X-User-Id`.
- **`storage.DataMissing` → 503 `data_missing`.** Used when the semester label
  is not yet seeded (fresh deploy). The frontend treats this as "backend up
  but cold" and falls back to its bundled snapshot.

---

## Things deliberately *not* shipped on `final-v1`

- Real authentication (sessions, OAuth, roles). Identity is an opaque
  client-minted UUID; admin is a shared bearer token.
- Per-cell change-request scope finer than "batch" / "class".
- Webhooks/event stream for moderation. Admins poll
  `GET /admin/change-requests?status=pending`.
- Saturday rendering in the frontend grid (data is parsed and stored, but
  the UI keeps `DAYS = Mon..Fri`).
- A dedicated audit log for admin writes (commits on the JSON mirror are
  the only trail, and only when `GIT_AUTO_COMMIT=1`).

---

## Compatibility contract for the takeover team

Detail in [API.md](API.md#compatibility-contract-for-the-other-team). The
short version:

- Do not rename or remove fields in any response documented in `API.md`.
- Keep the `{detail: {error, code}}` error envelope.
- Keep the `X-User-Id` header semantics — opaque, regex-validated, no
  401 for unknown ids.
- The frontend ships a bundled snapshot in `mlsc-timetable/public/fallback/`
  that mirrors the on-disk JSON layout (`batch.json`, `current.json`,
  `timetable/<batch>.json`). Keep that layout stable, or coordinate before
  changing it.
