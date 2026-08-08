# Public canonical timetable snapshots

MongoDB is the source of truth. Public snapshots are a rebuildable read model
for anonymous and signed-in clients. Each batch is written as immutable JSON;
`v1/manifest.json` is updated only after the object write succeeds.

## Object layout

```text
v1/manifest.json
v1/timetables/3C11/19-a81f2c9d54e712ab.json
```

The manifest includes the active object path, source timetable revision,
SHA-256 ETag, and generation timestamp. Catalog, Curriculum Library, teacher
visibility and semester metadata are already projected into each object.

## Development

The default backend is local:

```env
PUBLIC_SNAPSHOT_BACKEND=local
PUBLIC_SNAPSHOT_DIR=./data/public
```

Build every current snapshot once:

```http
POST /admin/public-snapshots/rebuild
Authorization: Bearer <admin token or Clerk JWT>
Content-Type: application/json

{}
```

Rebuild one batch with `{"batch":"3C11"}`. Local objects are served by the
backend at `/public/v1/*` with ETags and immutable cache headers.

## Cloudflare R2 production setup

Create a private S3-compatible R2 bucket such as
`mlsc-public-projections`, create a least-privilege object read/write token,
connect a public custom domain through Cloudflare, and allow browser `GET` /
`HEAD` requests from the exact production frontend origin in the bucket CORS
policy. Configure only the backend with the R2 credentials:

```env
PUBLIC_SNAPSHOT_BACKEND=r2
PUBLIC_SNAPSHOT_BASE_URL=https://data.timetable.mlsctiet.com
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=mlsc-public-projections
R2_ACCESS_KEY_ID=<secret>
R2_SECRET_ACCESS_KEY=<secret>
```

Configure the frontend build with the public custom domain only:

```env
VITE_PUBLIC_DATA_URL=https://data.timetable.mlsctiet.com
```

Never expose R2 credentials through a `VITE_` variable. Keep source Excel/PDF
uploads in a separate private bucket; this public bucket contains projections
only.

## Publication and freshness

- Timetable writes publish the affected batch after the MongoDB commit.
- Ingestion review publishes every changed batch.
- Catalog, Library and teacher-visibility changes rebuild only the batches
  whose projected output can change. Semester metadata remains a global
  rebuild because it can change every batch's curriculum resolution.
- Multi-batch rebuilds upload all immutable objects and activate them with one
  manifest write, avoiding one mutable R2 operation per affected batch.
- Browsers revalidate the manifest every 60 seconds and when the tab regains
  focus. Immutable batch objects are cached for one year.
- If publication fails, the previous manifest revision stays active. The
  committed MongoDB write is retained and the rebuild endpoint safely retries.

## Personal timetable migration

The new `/me/preferences/{batch}` path reads only
`personal_timetable_customizations_v2`; it never queries the legacy
`overrides` collection. Before enabling the split frontend in an environment
with legacy rows, run the existing migration in dry-run mode, review the
counts/conflicts, then apply it with the production safety flags documented by
the script:

```bash
python scripts/migrate_personal_overrides_v2.py \
  --mongo-url "$MONGODB_URL" \
  --database "$MONGODB_DB"
```

Keep the compatibility `/me/timetable` route during the rollout. Remove it
only after production confirms that all clients use canonical snapshots plus
the preference delta.
