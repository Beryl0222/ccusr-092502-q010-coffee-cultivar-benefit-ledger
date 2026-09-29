"""事件存储：幂等、版本连续与中断恢复。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.store import DuplicateEvent, EventStore, EventStoreError


def evt(event_id: str, agg: str, version: int, etype: str = "PARTY_REGISTERED") -> dict:
    return {
        "event_id": event_id, "event_type": etype,
        "occurred_at": "2026-09-24T09:00:00+08:00",
        "aggregate_id": agg, "version": version,
        "payload": {"party_id": "x", "name": "n", "roles": []},
    }


class EventStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.events = {"PARTY_REGISTERED"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_version_must_be_contiguous(self) -> None:
        store = EventStore(self.path, self.events)
        store.append(evt("e1", "a", 1))
        with self.assertRaises(EventStoreError):
            store.append(evt("e2", "a", 3))
        store.append(evt("e3", "b", 1))
        store.append(evt("e4", "a", 2))

    def test_resend_same_event_id_is_idempotent(self) -> None:
        store = EventStore(self.path, self.events)
        store.append(evt("e1", "a", 1))
        with self.assertRaises(DuplicateEvent):
            store.append(evt("e1", "a", 1))
        self.assertEqual(len(store.events()), 1)

    def test_reload_recovers_state(self) -> None:
        store = EventStore(self.path, self.events)
        store.append(evt("e1", "a", 1))
        store.append(evt("e2", "a", 2))
        store.append(evt("e3", "b", 1))
        again = EventStore(self.path, self.events)
        again.load()
        self.assertEqual(again.version_of("a"), 2)
        self.assertTrue(again.seen("e2"))
        self.assertEqual(len(again.events()), 3)
        # 恢复后继续写入，版本号正确衔接
        again.append(evt("e4", "a", 3))

    def test_unknown_event_rejected(self) -> None:
        store = EventStore(self.path, {"OTHER"})
        with self.assertRaises(EventStoreError):
            store.append(evt("e1", "a", 1))

    def test_payload_is_jsonl(self) -> None:
        store = EventStore(self.path, self.events)
        store.append(evt("e1", "a", 1))
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event_id"], "e1")


if __name__ == "__main__":
    unittest.main()
