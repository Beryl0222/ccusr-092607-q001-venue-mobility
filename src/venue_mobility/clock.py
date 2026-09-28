"""可控时钟：所有确认、候车、失约与升级节点都依据它推进。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class ClockError(ValueError):
    """时钟推进非法。"""


@dataclass
class ControlledClock:
    """只允许向未来推进的时钟；恢复时用最后记录的时刻重建。"""

    now: datetime

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ClockError("时钟初始时刻必须携带时区")

    def advance(self, minutes: int = 0, seconds: int = 0) -> datetime:
        delta = timedelta(minutes=minutes, seconds=seconds)
        if delta <= timedelta(0):
            raise ClockError("时钟只能向未来推进")
        self.now += delta
        return self.now

    def advance_to(self, target: datetime) -> datetime:
        if target.tzinfo is None or target.utcoffset() is None:
            raise ClockError("目标时刻必须携带时区")
        if target < self.now:
            raise ClockError(f"时钟不能回拨：{target.isoformat()} 早于 {self.now.isoformat()}")
        self.now = target
        return self.now
