import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from datetime import timedelta

from venue_mobility.events import Clock, DuplicateEvent, Event, EventStore, VersionConflict


def make_event(event_id: str = "e1", aggregate_id: str = "a1", version: int = 1,
               event_type: str = "REQUEST_ACCEPTED",
               aggregate_type: str = "travel_request") -> Event:
    return Event(
        event_id=event_id, event_type=event_type,
        aggregate_type=aggregate_type, aggregate_id=aggregate_id,
        occurred_at=Clock().now(), version=version, payload={"x": 1},
    )


class ClockTests(unittest.TestCase):
    def test_clock_advances_forward_only(self) -> None:
        clock = Clock()
        start = clock.now()
        clock.advance(timedelta(minutes=5))
        self.assertGreater(clock.now(), start)
        with self.assertRaises(ValueError):
            clock.advance(timedelta(minutes=-1))

    def test_clock_requires_timezone(self) -> None:
        from datetime import datetime
        with self.assertRaises(ValueError):
            Clock(datetime(2026, 9, 27, 8, 0))


class EventStoreTests(unittest.TestCase):
    def test_version_must_be_contiguous_per_aggregate(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "a1", 1))
        with self.assertRaises(VersionConflict):
            store.append(make_event("e2", "a1", 3))
        store.append(make_event("e3", "a1", 2))
        self.assertEqual(2, store.version_of("a1"))

    def test_event_id_is_idempotent_key(self) -> None:
        store = EventStore()
        store.append(make_event("e1"))
        with self.assertRaises(DuplicateEvent):
            store.append(make_event("e1", version=2))

    def test_aggregates_advance_independently(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "a1", 1))
        store.append(make_event("e2", "a2", 1))
        store.append(make_event("e3", "a1", 2))
        self.assertEqual(2, store.version_of("a1"))
        self.assertEqual(1, store.version_of("a2"))

    def test_batch_is_all_or_nothing(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "a1", 1))
        batch = [
            make_event("e2", "a1", 2),
            make_event("e2", "a2", 1),  # 重复 id：整批拒绝
        ]
        with self.assertRaises(DuplicateEvent):
            store.append_many(batch)
        self.assertEqual(1, len(store))
        self.assertEqual(0, store.version_of("a2"))

    def test_jsonl_replay_restores_state(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "events.jsonl"
            store = EventStore(journal)
            store.append(make_event("e1", "a1", 1))
            store.append(make_event("e2", "a1", 2, event_type="JOURNEY_PLANNED",
                                    aggregate_type="journey_commitment"))
            reloaded = EventStore(journal)
            self.assertEqual(2, len(reloaded))
            self.assertEqual(2, reloaded.version_of("a1"))
            self.assertEqual("JOURNEY_PLANNED", reloaded.get("e2").event_type)

    def test_replay_rejects_corrupt_sequence(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "events.jsonl"
            good = make_event("e1", "a1", 1).to_dict()
            bad = make_event("e2", "a1", 5).to_dict()  # 断号
            journal.write_text(
                json.dumps(good, ensure_ascii=False) + "\n"
                + json.dumps(bad, ensure_ascii=False) + "\n",
                encoding="utf-8")
            with self.assertRaises(Exception):
                EventStore(journal)


if __name__ == "__main__":
    unittest.main()
