from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.public_snapshots import LocalObjectStore, _json_bytes, validate_snapshot_path


class PublicSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_store_round_trips_nested_immutable_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(Path(tmp))
            body = b'{"batch":"3C11"}'
            await store.put(
                "v1/timetables/3C11/19-a81f2c9d54e712ab.json",
                body,
                cache_control="public, immutable",
            )
            self.assertEqual(
                await store.get("v1/timetables/3C11/19-a81f2c9d54e712ab.json"),
                body,
            )

    async def test_local_store_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(Path(tmp))
            with self.assertRaises(ValueError):
                await store.put("../secret.json", b"{}", cache_control="public")

    def test_snapshot_path_validation_is_strict(self) -> None:
        code, key = validate_snapshot_path("3c11", "19-a81f2c9d54e712ab.json")
        self.assertEqual(code, "3C11")
        self.assertEqual(key, "v1/timetables/3C11/19-a81f2c9d54e712ab.json")
        for batch in ("4S1C", "2MCA2", "1STYEAR"):
            code, key = validate_snapshot_path(batch, "19-a81f2c9d54e712ab.json")
            self.assertEqual(code, batch)
            self.assertIn(f"/{batch}/", key)
        with self.assertRaises(ValueError):
            validate_snapshot_path("../../etc", "passwd")

    def test_canonical_json_is_deterministic(self) -> None:
        left = _json_bytes({"b": 2, "a": 1})
        right = _json_bytes({"a": 1, "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(json.loads(left), {"a": 1, "b": 2})
