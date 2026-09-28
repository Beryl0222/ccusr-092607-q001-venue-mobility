"""分散赛区通勤保障的静态事实与申报模型。

模型只描述事实，不持有任何在途状态；在途状态由 ``service.py`` 的事件流决定。
所有时刻均为携带时区的 ISO 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# 出行人角色
ATHLETE = "athlete"
MEDIA = "media"
STAFF = "staff"

# 保障通道；医疗与无障碍需求不可被普通优先级覆盖
NEED_STANDARD = "standard"
NEED_ACCESSIBLE = "accessible"
NEED_MEDICAL = "medical"

# 选项交通方式
MODE_TRANSIT = "transit"
MODE_VEHICLE = "vehicle"


@dataclass(frozen=True)
class VenueSession:
    """场馆竞赛/训练场次。"""

    venue_id: str
    session_id: str
    start_at: str
    latest_arrival_at: str
    security_zone: str


@dataclass(frozen=True)
class Residence:
    """代表团驻地。"""

    residence_id: str
    venue_id: str
    transit_minutes: int  # 到场馆安检外的公共交通在途时间（分钟）
    vehicle_minutes: int  # 到场馆安检外的专车在途时间（分钟）


@dataclass(frozen=True)
class TransitOption:
    """公共交通班次：必须准点乘降，停运后不可再选。"""

    transit_id: str
    residence_id: str
    venue_id: str
    departs_at: str
    arrives_at: str
    seats: int
    suspended: bool = False


@dataclass(frozen=True)
class SecuritySlot:
    """安检窗口：与座位、车辆同为可被争用的容量资源。"""

    slot_id: str
    venue_id: str
    security_zone: str
    starts_at: str
    ends_at: str
    capacity: int


@dataclass(frozen=True)
class Vehicle:
    """车辆与绑定司机资质。"""

    vehicle_id: str
    seats: int
    accessible: bool
    medical_graded: bool
    equipment_capacity: int  # 可同行的器材占位
    credential_zones: frozenset[str]
    based_at_residence: str | None = None


@dataclass(frozen=True)
class RouteClosure:
    """线路临时管制：在时间窗内指定驻地-场馆的专车不可用。"""

    closure_id: str
    residence_id: str
    venue_id: str
    starts_at: str
    ends_at: str


@dataclass(frozen=True)
class RequestInput:
    """代表团申报（一个申报对应一段行程）。"""

    business_ref: str
    person_id: str
    person_name: str
    role: str
    residence_id: str
    venue_id: str
    session_id: str
    party_size: int
    accessible: bool = False
    medical: bool = False
    equipment_units: int = 0
    # 可携带此前收到的通知幂等键；重复通知不重复派车
    notification_key: str | None = None

    @property
    def need_tier(self) -> str:
        if self.medical:
            return NEED_MEDICAL
        if self.accessible:
            return NEED_ACCESSIBLE
        return NEED_STANDARD


@dataclass(frozen=True)
class Scenario:
    """一次保障运行面对的全部静态事实。"""

    sessions: tuple[VenueSession, ...]
    residences: tuple[Residence, ...]
    transit: tuple[TransitOption, ...]
    security_slots: tuple[SecuritySlot, ...]
    vehicles: tuple[Vehicle, ...]
    closures: tuple[RouteClosure, ...] = ()

    def session(self, session_id: str) -> VenueSession | None:
        return next((s for s in self.sessions if s.session_id == session_id), None)

    def residence(self, residence_id: str) -> Residence | None:
        return next((r for r in self.residences if r.residence_id == residence_id), None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scenario":
        def as_tuple(key: str, factory: Any) -> tuple[Any, ...]:
            return tuple(factory(item) for item in data.get(key, ()))

        vehicles = tuple(
            Vehicle(
                vehicle_id=item["vehicle_id"],
                seats=item["seats"],
                accessible=item.get("accessible", False),
                medical_graded=item.get("medical_graded", False),
                equipment_capacity=item.get("equipment_capacity", 0),
                credential_zones=frozenset(item.get("credential_zones", ())),
                based_at_residence=item.get("based_at_residence"),
            )
            for item in data.get("vehicles", ())
        )
        return cls(
            sessions=as_tuple(
                "sessions",
                lambda item: VenueSession(
                    venue_id=item["venue_id"],
                    session_id=item["session_id"],
                    start_at=item["start_at"],
                    latest_arrival_at=item["latest_arrival_at"],
                    security_zone=item["security_zone"],
                ),
            ),
            residences=as_tuple(
                "residences",
                lambda item: Residence(
                    residence_id=item["residence_id"],
                    venue_id=item["venue_id"],
                    transit_minutes=item["transit_minutes"],
                    vehicle_minutes=item["vehicle_minutes"],
                ),
            ),
            transit=as_tuple(
                "transit",
                lambda item: TransitOption(
                    transit_id=item["transit_id"],
                    residence_id=item["residence_id"],
                    venue_id=item["venue_id"],
                    departs_at=item["departs_at"],
                    arrives_at=item["arrives_at"],
                    seats=item["seats"],
                    suspended=item.get("suspended", False),
                ),
            ),
            security_slots=as_tuple(
                "security_slots",
                lambda item: SecuritySlot(
                    slot_id=item["slot_id"],
                    venue_id=item["venue_id"],
                    security_zone=item["security_zone"],
                    starts_at=item["starts_at"],
                    ends_at=item["ends_at"],
                    capacity=item["capacity"],
                ),
            ),
            vehicles=vehicles,
            closures=as_tuple(
                "closures",
                lambda item: RouteClosure(
                    closure_id=item["closure_id"],
                    residence_id=item["residence_id"],
                    venue_id=item["venue_id"],
                    starts_at=item["starts_at"],
                    ends_at=item["ends_at"],
                ),
            ),
        )
