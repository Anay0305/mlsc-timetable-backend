from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from tempfile import NamedTemporaryFile

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from starlette.concurrency import run_in_threadpool

from timetable_api.config import Settings
from timetable_api.schemas import (
    BatchListResponse,
    ClassSearchResponse,
    Day,
    HealthResponse,
    MetadataResponse,
    TimetableResponse,
    WorkbookUploadResponse,
)
from timetable_api._store import TimetableStore


ALLOWED_WORKBOOK_EXTENSIONS = {".xlsx", ".xlsm"}
WORKBOOK_UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_WORKBOOK_UPLOAD_BYTES = 25 * 1024 * 1024


def create_app(store: TimetableStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = Settings.from_environment()
        application.state.settings = settings
        if store is not None:
            application.state.store = store
        else:
            application.state.store = TimetableStore.empty()
        yield

    application = FastAPI(
        title="MLSC Timetable API",
        version="1.0.0",
        description="API for parsed Thapar timetable data.",
        lifespan=lifespan,
    )

    @application.get("/health", response_model=HealthResponse, tags=["System"])
    def health(timetable_store: TimetableStore = Depends(get_store)) -> HealthResponse:
        return HealthResponse(
            status="ok",
            storage="memory",
            batches=timetable_store.batch_count,
            classes=timetable_store.class_count,
        )

    @application.get(
        "/api/v1/batches",
        response_model=BatchListResponse,
        tags=["Timetables"],
    )
    def list_batches(timetable_store: TimetableStore = Depends(get_store)) -> BatchListResponse:
        batches = timetable_store.batches()
        return BatchListResponse(count=len(batches), batches=batches)

    @application.get(
        "/api/v1/batches/{batch}/timetable",
        response_model=TimetableResponse,
        tags=["Timetables"],
    )
    def get_timetable(
        batch: str,
        day: Day | None = None,
        timetable_store: TimetableStore = Depends(get_store),
    ) -> TimetableResponse:
        normalized_batch = batch.upper()
        days = timetable_store.timetable(normalized_batch, day)
        if days is None:
            raise HTTPException(
                status_code=404,
                detail=f"Batch '{normalized_batch}' was not found",
            )
        return TimetableResponse(
            batch=normalized_batch,
            source_sheet=timetable_store.source_sheet(normalized_batch),
            days=days,
        )

    @application.get("/api/v1/classes", response_model=ClassSearchResponse, tags=["Classes"])
    def search_classes(
        subject_code: str | None = None,
        day: Day | None = None,
        class_type: str | None = Query(default=None, alias="type"),
        query: str | None = Query(default=None, alias="q", min_length=1),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1_000),
        timetable_store: TimetableStore = Depends(get_store),
    ) -> ClassSearchResponse:
        classes = timetable_store.search_classes(
            subject_code=subject_code,
            day=day,
            class_type=class_type,
            query=query,
        )
        page = classes[offset : offset + limit]
        return ClassSearchResponse(
            count=len(page),
            total=len(classes),
            offset=offset,
            limit=limit,
            classes=page,
        )

    @application.get("/api/v1/metadata", response_model=MetadataResponse, tags=["System"])
    def metadata(timetable_store: TimetableStore = Depends(get_store)) -> MetadataResponse:
        return _metadata_response(timetable_store)

    @application.post(
        "/api/v1/admin/workbook",
        response_model=WorkbookUploadResponse,
        tags=["Admin"],
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_admin_secret)],
    )
    async def upload_workbook(
        request: Request,
        file: UploadFile = File(...),
    ) -> WorkbookUploadResponse:
        original_filename = _validate_workbook_upload(file)
        temporary_path = await _save_upload_to_tempfile(file, original_filename)

        try:
            next_store = await run_in_threadpool(
                TimetableStore.from_workbook,
                temporary_path,
                original_filename,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Workbook could not be parsed: {exc}",
            ) from exc
        finally:
            temporary_path.unlink(missing_ok=True)
            await file.close()

        if next_store.batch_count == 0 or next_store.class_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workbook did not contain any parsable timetable data.",
            )

        request.app.state.store = next_store
        return WorkbookUploadResponse(
            status="updated",
            **_metadata_response(next_store).model_dump(),
        )

    return application


def get_store(request: Request) -> TimetableStore:
    return request.app.state.store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _metadata_response(timetable_store: TimetableStore) -> MetadataResponse:
    return MetadataResponse(
        source=timetable_store.source,
        loaded_at=timetable_store.loaded_at,
        sheets=timetable_store.sheets,
        batches=timetable_store.batch_count,
        classes=timetable_store.class_count,
    )


def require_admin_secret(
    settings: Settings = Depends(get_settings),
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> None:
    if not settings.admin_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin workbook uploads are disabled; set TIMETABLE_ADMIN_SECRET.",
        )
    if x_admin_secret is None or not compare_digest(x_admin_secret, settings.admin_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin secret.",
        )


def _validate_workbook_upload(file: UploadFile) -> str:
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload must include a filename.",
        )

    filename = Path(file.filename).name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_WORKBOOK_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_WORKBOOK_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload must be an Excel workbook ({allowed}).",
        )
    return filename


async def _save_upload_to_tempfile(file: UploadFile, filename: str) -> Path:
    suffix = Path(filename).suffix.lower()
    size = 0

    with NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
        temporary_path = Path(temporary_file.name)
        while chunk := await file.read(WORKBOOK_UPLOAD_CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_WORKBOOK_UPLOAD_BYTES:
                temporary_file.close()
                temporary_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Workbook upload is too large.",
                )
            temporary_file.write(chunk)

    if size == 0:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded workbook is empty.",
        )

    return temporary_path


app = create_app()
