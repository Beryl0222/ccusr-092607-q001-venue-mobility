"""事件信封、可控时钟与只追加事件存储。

事件是系统内唯一的事实来源：状态只能通过重放事件得到，事件一旦写入
不可修改，纠偏必须追加新事件。存储同时维护内存索引与 JSONL 日志，进程
重启后重放日志即可恢复全部未竟的保障义务。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


class EventError(Exception):
    """事件层基础异常。"""


class DuplicateEvent(EventError):
    """相同 event_id 已存在；重放与幂等提交靠调用方区分语义。"""


class VersionConflict(EventError):
    """聚合版本断号或重复。"""

    def __init__(self, aggregate_id: str, expected: int, got: int) -> None:
        super().__init__(f"聚合 {aggregate_id} 期望版本 {expected}，实际 {got}")
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.got = got


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        occurred = raw["occurred_at"]
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred)
        return cls(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            aggregate_type=raw["aggregate_type"],
            aggregate_id=raw["aggregate_id"],
            occurred_at=occurred,
            version=raw["version"],
            payload=dict(raw.get("payload") or {}),
        )


class Clock:
    """可控时钟：测试与调度节点显式推进，生产环境可换成系统时钟。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime.now(timezone.utc)
        if start.tzinfo is None:
            raise ValueError("时钟起点必须携带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta <= timedelta(0):
            raise ValueError("时钟只能向前推进")
        self._now += delta
        return self._now

    def advance_to(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("目标时刻必须携带时区")
        if moment < self._now:
            raise ValueError("时钟不能回拨")
        self._now = moment
        return self._now


class EventStore:
    """按聚合维护版本与索引的只追加存储，可镜像到 JSONL 文件。"""

    def __init__(self, journal: str | os.PathLike[str] | None = None) -> None:
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._versions: dict[str, int] = {}
        self._by_aggregate: dict[str, list[Event]] = {}
        self._journal = Path(journal) if journal else None
        if self._journal is not None and self._journal.exists():
            self._replay(self._journal)

    # -- 写入 -------------------------------------------------------------

    def append(self, event: Event) -> Event:
        duplicate = self._by_id.get(event.event_id)
        if duplicate is not None:
            raise DuplicateEvent(event.event_id)
        expected = self._versions.get(event.aggregate_id, 0) + 1
        if event.version != expected:
            raise VersionConflict(event.aggregate_id, expected, event.version)
        self._index(event)
        if self._journal is not None:
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def append_many(self, events: Iterable[Event]) -> list[Event]:
        """整批提交：先全部校验（幂等与版本），再逐条落盘。

        任一事件不合法则整批拒绝，不会出现占了车却没写释放事件的半状态。
        """
        events = list(events)
        seen_ids: set[str] = set()
        provisional_versions = dict(self._versions)
        for event in events:
            if event.event_id in self._by_id or event.event_id in seen_ids:
                raise DuplicateEvent(event.event_id)
            expected = provisional_versions.get(event.aggregate_id, 0) + 1
            if event.version != expected:
                raise VersionConflict(event.aggregate_id, expected, event.version)
            provisional_versions[event.aggregate_id] = event.version
            seen_ids.add(event.event_id)
        committed: list[Event] = []
        for event in events:
            try:
                committed.append(self.append(event))
            except DuplicateEvent:  # 极小窗口下的并发重放
                continue
        return committed

    def _index(self, event: Event) -> None:
        self._events.append(event)
        self._by_id[event.event_id] = event
        self._versions[event.aggregate_id] = event.version
        self._by_aggregate.setdefault(event.aggregate_id, []).append(event)

    # -- 读取 -------------------------------------------------------------

    def get(self, event_id: str) -> Event | None:
        return self._by_id.get(event_id)

    def stream(self) -> Iterator[Event]:
        return iter(self._events)

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def events_for(self, aggregate_id: str) -> list[Event]:
        return list(self._by_aggregate.get(aggregate_id, ()))

    def __len__(self) -> int:
        return len(self._events)

    # -- 恢复 -------------------------------------------------------------

    def _replay(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = Event.from_dict(json.loads(line))
                expected = self._versions.get(event.aggregate_id, 0) + 1
                if event.version != expected:
                    raise EventError(
                        f"日志损坏：聚合 {event.aggregate_id} 版本断号"
                    )
                if event.event_id in self._by_id:
                    # 重复尾部（上次写入落盘后崩溃于确认前）直接跳过
                    continue
                self._index(event)
