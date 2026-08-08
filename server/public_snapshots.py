"""Revisioned public timetable snapshots.

MongoDB remains authoritative.  This module materializes the fully projected,
teacher-redacted public read model into immutable JSON objects and updates a
small mutable manifest only after the object write succeeds.

The local backend is useful for development and emergency single-host
deployments.  Production can use Cloudflare R2 through its S3-compatible API;
the frontend then reads the configured public R2 custom domain directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from server.config import Settings, get_settings

logger = logging.getLogger(__name__)

MANIFEST_KEY = "v1/manifest.json"
_BATCH_RE = re.compile(r"^[0-9][A-Z0-9]{1,15}$")
_SNAPSHOT_FILE_RE = re.compile(r"^[0-9]+-[a-f0-9]{16}\.json$")
_publish_lock = asyncio.Lock()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class ObjectStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, body: bytes, *, cache_control: str) -> None: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("snapshot key escapes configured directory")
        return path

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None

    async def put(self, key: str, body: bytes, *, cache_control: str) -> None:
        del cache_control  # HTTP caching is attached by the local public router.
        path = self._path(key)
        await asyncio.to_thread(_atomic_write, path, body)


class R2ObjectStore:
    def __init__(self, settings: Settings) -> None:
        missing = [
            name
            for name, value in {
                "R2_ENDPOINT_URL": settings.r2_endpoint_url,
                "R2_BUCKET": settings.r2_bucket,
                "R2_ACCESS_KEY_ID": settings.r2_access_key_id,
                "R2_SECRET_ACCESS_KEY": settings.r2_secret_access_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"R2 snapshot backend is missing: {', '.join(missing)}")
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise RuntimeError("boto3 is required for PUBLIC_SNAPSHOT_BACKEND=r2") from exc
        self.bucket = str(settings.r2_bucket)
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )

    async def get(self, key: str) -> bytes | None:
        def _read() -> bytes | None:
            try:
                result = self.client.get_object(Bucket=self.bucket, Key=key)
            except self.client.exceptions.NoSuchKey:
                return None
            except Exception as exc:
                response = getattr(exc, "response", {})
                if response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return None
                raise
            return result["Body"].read()

        return await asyncio.to_thread(_read)

    async def put(self, key: str, body: bytes, *, cache_control: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            CacheControl=cache_control,
        )


def get_store(settings: Settings | None = None) -> ObjectStore:
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "r2":
        return R2ObjectStore(settings)
    if settings.public_snapshot_backend == "local":
        return LocalObjectStore(settings.public_snapshot_dir)
    raise RuntimeError("public snapshot publishing is disabled")


async def read_object(key: str, settings: Settings | None = None) -> bytes | None:
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "disabled":
        return None
    return await get_store(settings).get(key)


async def publish_batch(batch: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Build and atomically advertise one batch's current public projection."""
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "disabled":
        return None

    code = "".join(ch for ch in str(batch).strip().upper() if ch.isalnum())
    if not _BATCH_RE.fullmatch(code):
        raise ValueError(f"invalid batch code: {batch!r}")
    object_key, body, entry = await _build_batch_object(code, settings)
    store = get_store(settings)

    async with _publish_lock:
        # Immutable object first. A failed write can never advertise a partial
        # or missing revision through the manifest.
        await store.put(
            object_key,
            body,
            cache_control="public, max-age=31536000, immutable",
        )
        manifest = await _read_manifest(store)
        now = datetime.now(timezone.utc).isoformat()
        manifest.setdefault("batches", {})[code] = entry
        manifest["schema_version"] = 1
        manifest["generated_at"] = now
        await store.put(
            MANIFEST_KEY,
            _json_bytes(manifest),
            cache_control="public, max-age=30, s-maxage=30, stale-while-revalidate=300",
        )
    logger.info("Published public snapshot %s -> %s", code, object_key)
    return entry


async def publish_all(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "disabled":
        return {"published": 0, "skipped": True}
    from server import storage

    return await publish_batches(
        await storage.read_batch_list(),
        settings=settings,
        replace_manifest=True,
    )


async def publish_batches(
    batches: list[str] | set[str],
    settings: Settings | None = None,
    *,
    replace_manifest: bool = False,
) -> dict[str, Any]:
    """Publish a dependency set with one manifest read and activation.

    ``replace_manifest`` is reserved for full rebuilds and prunes batches no
    longer present in MongoDB. Failed current batches retain their previous
    active entry so a rebuild can never turn a transient failure into an
    outage.
    """
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "disabled":
        return {"published": 0, "skipped": True}
    normalized = sorted({
        "".join(ch for ch in str(batch).strip().upper() if ch.isalnum())
        for batch in batches
    })
    if not normalized and not replace_manifest:
        return {"published": 0, "batches": [], "failures": []}

    published: list[str] = []
    failures: list[dict[str, str]] = []
    store = get_store(settings)
    # One manifest read and one atomic activation for the whole rebuild avoids
    # hundreds of mutable-object writes and prevents a partially rebuilt
    # dependency set becoming active batch by batch.
    async with _publish_lock:
        manifest = await _read_manifest(store)
        current_batches = dict(manifest.get("batches") or {})
        next_batches = (
            {batch: current_batches[batch] for batch in normalized if batch in current_batches}
            if replace_manifest
            else current_batches
        )
        for batch in normalized:
            try:
                if not _BATCH_RE.fullmatch(batch):
                    raise ValueError(f"invalid batch code: {batch!r}")
                object_key, body, entry = await _build_batch_object(batch, settings)
                await store.put(
                    object_key,
                    body,
                    cache_control="public, max-age=31536000, immutable",
                )
                next_batches[batch] = entry
                published.append(batch)
            except Exception as exc:  # keep a bulk rebuild inspectable and retryable
                logger.exception("Could not publish public snapshot for %s", batch)
                failures.append({"batch": batch, "error": str(exc)})
        now = datetime.now(timezone.utc).isoformat()
        manifest.update({"schema_version": 1, "generated_at": now, "batches": next_batches})
        await store.put(
            MANIFEST_KEY,
            _json_bytes(manifest),
            cache_control="public, max-age=30, s-maxage=30, stale-while-revalidate=300",
        )
    return {"published": len(published), "batches": published, "failures": failures}


async def _build_batch_object(
    code: str,
    settings: Settings,
) -> tuple[str, bytes, dict[str, Any]]:
    # Local imports avoid a storage -> snapshot -> storage import cycle.
    from server import storage
    from server.db.models import TimetableDoc

    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        raise storage.BatchNotFound(code)
    payload = await storage.read_timetable(code, include_unavailable=True)
    payload["snapshot"] = {
        "schema_version": 1,
        "source_revision": int(doc.revision or 0),
    }
    body = _json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    filename = f"{int(doc.revision or 0)}-{digest[:16]}.json"
    object_key = f"v1/timetables/{code}/{filename}"
    now = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "revision": int(doc.revision or 0),
        "etag": digest,
        "path": object_key,
        "generated_at": now,
    }
    if settings.public_snapshot_base_url:
        entry["url"] = f"{settings.public_snapshot_base_url}/{object_key}"
    return object_key, body, entry


async def unpublish_batch(batch: str, settings: Settings | None = None) -> bool:
    """Remove a batch from the active manifest while retaining old objects."""
    settings = settings or get_settings()
    if settings.public_snapshot_backend == "disabled":
        return False
    code = "".join(ch for ch in str(batch).strip().upper() if ch.isalnum())
    if not _BATCH_RE.fullmatch(code):
        raise ValueError(f"invalid batch code: {batch!r}")
    store = get_store(settings)
    async with _publish_lock:
        manifest = await _read_manifest(store)
        removed = manifest.setdefault("batches", {}).pop(code, None) is not None
        if not removed:
            return False
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
        await store.put(
            MANIFEST_KEY,
            _json_bytes(manifest),
            cache_control="public, max-age=30, s-maxage=30, stale-while-revalidate=300",
        )
        return True


async def _read_manifest(store: ObjectStore) -> dict[str, Any]:
    raw = await store.get(MANIFEST_KEY)
    if not raw:
        return {"schema_version": 1, "batches": {}}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid public snapshot manifest")
        return {"schema_version": 1, "batches": {}}
    if not isinstance(value, dict) or not isinstance(value.get("batches"), dict):
        return {"schema_version": 1, "batches": {}}
    return value


def validate_snapshot_path(batch: str, filename: str) -> tuple[str, str]:
    code = "".join(ch for ch in batch.strip().upper() if ch.isalnum())
    if not _BATCH_RE.fullmatch(code) or not _SNAPSHOT_FILE_RE.fullmatch(filename):
        raise ValueError("invalid public snapshot path")
    return code, f"v1/timetables/{code}/{filename}"


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(body)
    os.replace(temporary, path)
