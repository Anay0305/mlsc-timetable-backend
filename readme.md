# mlsc-timetable-backend

Backend for the MLSC timetable website and mobile app.

## Overview

`mlsc-timetable-backend` is the backend layer for parsing, storing, and serving Thapar timetable data.

FastAPI starts with an empty in-memory store. The admin panel uploads an Excel workbook through the REST API, the backend privately parses it, and the parsed timetable becomes available to the website and mobile app.

The workbook parser is an internal implementation detail under `timetable_api/_parser`. It is not a public Python interface; consumers access timetable data only through the REST API.

## Current Scope

- Read-only timetable REST endpoints
- Admin-protected workbook upload and refresh
- In-memory batch and class lookup
- Subject, day, type, and free-text class filtering
- OpenAPI and Swagger documentation

## Setup

Requires Python 3.11+.

```bash
python -m pip install -e .
```

## Run the API

```bash
python -m uvicorn timetable_api.app:app --reload
```

No workbook is read from disk during startup. Timetable data is loaded only through the admin upload endpoint and retained in memory. Keep the default single worker because each additional worker has its own in-memory store.

Interactive API documentation is available at:

- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

Set `TIMETABLE_ADMIN_SECRET` to enable admin workbook uploads:

```powershell
$env:TIMETABLE_ADMIN_SECRET = "replace-with-a-long-random-secret"
python -m uvicorn timetable_api.app:app --reload
```

## REST API

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Check API health and in-memory record counts. |
| `GET` | `/api/v1/batches` | List all available batch codes. |
| `GET` | `/api/v1/batches/{batch}/timetable` | Get a batch's weekly timetable. |
| `GET` | `/api/v1/batches/{batch}/timetable?day=MONDAY` | Get one day of a batch's timetable. |
| `GET` | `/api/v1/classes` | Search and filter classes. |
| `GET` | `/api/v1/metadata` | Get workbook source and load metadata. |
| `POST` | `/api/v1/admin/workbook` | Upload a new Excel workbook, parse it, and replace the live in-memory timetable. |

Before the first successful upload, read endpoints return empty results or `404` for batch-specific lookups.

### Class filters

`GET /api/v1/classes` accepts these optional query parameters:

| Parameter | Example | Description |
| --- | --- | --- |
| `subject_code` | `UPH013P` | Match a primary or elective subject code. |
| `day` | `MONDAY` | Match one weekday. |
| `type` | `PRACTICAL` | Match the parsed class type. |
| `q` | `G312` | Search subject names, room/teacher tokens, and raw parser values. |
| `offset` | `0` | Skip matching classes for pagination. |
| `limit` | `100` | Return up to 1,000 classes per request. |

Filters can be combined:

```http
GET /api/v1/classes?subject_code=UPH013P&day=MONDAY&q=G312
```

When a batch exists on multiple workbook sheets, the API uses the last non-empty version in workbook order. Timetable responses include `source_sheet` so the selected version is visible.

### Admin workbook upload

`POST /api/v1/admin/workbook` accepts a multipart Excel upload under the `file` field. It requires the admin secret header:

```http
X-Admin-Secret: replace-with-a-long-random-secret
```

Example:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/workbook" \
  -H "X-Admin-Secret: replace-with-a-long-random-secret" \
  -F "file=@UG, PG TIME TABLE JAN TO MAY 2026.xlsx"
```

Supported workbook extensions are `.xlsx` and `.xlsm`. Uploads are capped at 25 MB.

This endpoint is the only way to load or refresh timetable data. The API parses the uploaded workbook into a fresh in-memory store first. If parsing fails or produces no timetable data, the currently loaded timetable remains unchanged. If parsing succeeds, the live store is replaced and the response returns the new source filename, load time, sheet count, batch count, and class count.
