# Google Cloud Build

The backend is built from the root `Dockerfile`, pushed to Artifact Registry,
and deployed to Cloud Run by `cloudbuild.yaml`.

Cloud Build is the CI/CD layer; Cloud Run is the managed HTTPS service that
runs the container.

## One-Time Setup

Set the project and enable the required APIs:

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

Create the Docker repository once:

```bash
gcloud artifacts repositories create mlsc \
  --repository-format=docker \
  --location=asia-south1 \
  --description="MLSC backend images"
```

Grant the Cloud Build service account permission to deploy and publish images:

```bash
PROJECT_ID="$(gcloud config get-value project)"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/run.admin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/artifactregistry.writer"
gcloud iam service-accounts add-iam-policy-binding \
  "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/iam.serviceAccountUser"
```

## Deploy

From the backend repository:

```bash
gcloud builds submit --config=cloudbuild.yaml .
```

Override the defaults when needed:

```bash
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_REGION=asia-south1,_REPOSITORY=mlsc,_SERVICE=mlsc-timetable-backend .
```

The service listens on the Cloud Run-provided `$PORT` and exposes:

```text
GET /healthz
```

## Secrets And Environment

Configure these on the Cloud Run service. Keep credentials in Secret Manager:

```text
MONGODB_URL
MONGODB_DB
ADMIN_TOKEN
ADMIN_EMAILS
CLERK_ISSUER
CLERK_JWKS_URL
CORS_ORIGINS
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
GOOGLE_OAUTH_REDIRECT_URI
CALENDAR_TOKEN_KEY
JSON_MIRROR
DATA_DIR
```

Set the production CORS origins exactly:

```text
https://timetable.mlsctiet.com,https://www.timetable.mlsctiet.com
```

`PORT` is injected by Cloud Run. Do not hard-code it in the deployment
configuration.

For an existing service, update non-secret environment variables with:

```bash
gcloud run services update mlsc-timetable-backend \
  --region=asia-south1 \
  --set-env-vars='MONGODB_DB=mlsc_timetable,CORS_ORIGINS=https://timetable.mlsctiet.com,https://www.timetable.mlsctiet.com,JSON_MIRROR=0'
```

Use `--update-secrets` for secret-backed values rather than passing credentials
on the command line.
