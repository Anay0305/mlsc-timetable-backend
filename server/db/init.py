"""Beanie/Motor lifecycle helpers."""

from __future__ import annotations

import logging

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from server.config import Settings, get_settings
from server.db.models import ALL_DOCUMENTS

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None


async def init_db(settings: Settings | None = None) -> None:
    """Connect to Mongo and register Beanie document classes. Idempotent."""
    global _client
    settings = settings or get_settings()
    if _client is not None:
        return
    _client = AsyncIOMotorClient(
        settings.mongodb_url,
        uuidRepresentation="standard",
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=settings.mongodb_min_pool_size,
        maxConnecting=settings.mongodb_max_connecting,
        waitQueueTimeoutMS=settings.mongodb_wait_queue_timeout_ms,
        serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
        connectTimeoutMS=settings.mongodb_connect_timeout_ms,
        socketTimeoutMS=settings.mongodb_socket_timeout_ms,
        maxIdleTimeMS=settings.mongodb_max_idle_time_ms,
    )
    database = _client[settings.mongodb_db]
    await init_beanie(database=database, document_models=ALL_DOCUMENTS)
    logger.info(
        "Mongo connected: %s (db=%s, pool=%d..%d)",
        settings.mongodb_url,
        settings.mongodb_db,
        settings.mongodb_min_pool_size,
        settings.mongodb_max_pool_size,
    )


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
