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
    _client = AsyncIOMotorClient(settings.mongodb_url, uuidRepresentation="standard")
    database = _client[settings.mongodb_db]
    await init_beanie(database=database, document_models=ALL_DOCUMENTS)
    logger.info(
        "Mongo connected: %s (db=%s)",
        settings.mongodb_url,
        settings.mongodb_db,
    )


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
