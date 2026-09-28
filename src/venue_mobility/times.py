"""时间解析与比较工具：契约层与服务层共用。"""

from __future__ import annotations

from datetime import datetime, timedelta


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时刻缺少时区：{value}")
    return parsed


def iso(value: datetime) -> str:
    return value.isoformat()


def minutes_after(value: str, minutes: int) -> str:
    return iso(parse(value) + timedelta(minutes=minutes))
