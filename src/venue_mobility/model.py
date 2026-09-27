"""分散赛区通勤保障的领域对象。

主数据（场馆、驻地、路网、班次、车辆、司机、安检窗口、日程）来自启动配置；
申报与保障过程中的一切变化以事件表达。所有时间均为带时区的 ``datetime``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TravelerRole(str, Enum):
    ATHLETE = "athlete"      # 运动员
    MEDIA = "media"          # 媒体
    STAFF = "staff"          # 工作人员


ROLE_RANK = {
    TravelerRole.ATHLETE: 30,
    TravelerRole.MEDIA: 20,
    TravelerRole.STAFF: 10,
}

ROLE_LABEL = {
    TravelerRole.ATHLETE: "运动员",
    TravelerRole.MEDIA: "媒体",
    TravelerRole.STAFF: "工作人员",
}


@dataclass(frozen=True)
class Lodging:
    lodging_id: str
    node: str
    name: str


@dataclass(frozen=True)
class Venue:
    venue_id: str
    node: str
    name: str


@dataclass(frozen=True)
class Duty:
    """场馆侧的赛时义务：竞赛/训练/媒体通行等，ready_at 为必须就绪时刻。"""

    duty_id: str
    venue_id: str
    label: str
    ready_at: datetime
    window_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoadSegment:
    segment_id: str
    from_node: str
    to_node: str
    minutes: int


@dataclass(frozen=True)
class TransitTrip:
    """公共交通固定班次。"""

    trip_id: str
    route_name: str
    from_node: str
    to_node: str
    departs_at: datetime
    arrives_at: datetime
    seats: int
    wheelchair_spaces: int
    equipment_allowed: bool
    suspended: bool = False


@dataclass(frozen=True)
class SecurityWindow:
    """场馆安检窗口，slots 为可同时占用的安检时段容量。"""

    window_id: str
    venue_id: str
    opens_at: datetime
    closes_at: datetime
    slots: int
    accessible: bool
    closed: bool = False


@dataclass(frozen=True)
class Driver:
    driver_id: str
    name: str
    qualifications: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Vehicle:
    """可用车辆；requires 是该车司机必须具备的资质标签。"""

    vehicle_id: str
    kind: str                      # car 专车 / van 班车 / accessible_van 无障碍车
    base_node: str
    driver_id: str | None
    seats: int
    wheelchair_spaces: int
    equipment_capacity: int
    sharable: bool
    requires: frozenset[str] = frozenset()
    offline: bool = False


@dataclass(frozen=True)
class TravelRequest:
    """代表团申报（通勤通知）。"""

    request_id: str
    business_ref: str             # 代表团业务编号
    notice_id: str                # 通知编号：重复通知据此幂等
    person_ids: tuple[str, ...]
    role: TravelerRole
    party_size: int
    wheelchair_count: int
    medical: bool
    equipment_units: int
    origin_lodging_id: str
    duty_id: str
    required_arrival: datetime
    declared_at: datetime
    note: str = ""

    @property
    def hard_need(self) -> bool:
        """医疗或无障碍硬需求：普通优先级不得覆盖。"""
        return self.medical or self.wheelchair_count > 0

    def signature(self) -> tuple:
        """同业务编号的内容指纹：人员、时间、路线（起讫）不同即进入核查。"""
        return (
            tuple(sorted(self.person_ids)),
            self.required_arrival.isoformat(),
            self.origin_lodging_id,
            self.duty_id,
        )


class Registry:
    """主数据注册表。供给状态（停运/管制/下线/关闭）随事件回放重建。"""

    def __init__(self) -> None:
        self.lodgings: dict[str, Lodging] = {}
        self.venues: dict[str, Venue] = {}
        self.duties: dict[str, Duty] = {}
        self.segments: dict[str, RoadSegment] = {}
        self.trips: dict[str, TransitTrip] = {}
        self.windows: dict[str, SecurityWindow] = {}
        self.drivers: dict[str, Driver] = {}
        self.vehicles: dict[str, Vehicle] = {}
        self._restricted: set[str] = set()

    # -- 注册 -------------------------------------------------------------

    def add(self, obj: object) -> None:
        if isinstance(obj, Lodging):
            self.lodgings[obj.lodging_id] = obj
        elif isinstance(obj, Venue):
            self.venues[obj.venue_id] = obj
        elif isinstance(obj, Duty):
            self.duties[obj.duty_id] = obj
        elif isinstance(obj, RoadSegment):
            self.segments[obj.segment_id] = obj
        elif isinstance(obj, TransitTrip):
            self.trips[obj.trip_id] = obj
        elif isinstance(obj, SecurityWindow):
            self.windows[obj.window_id] = obj
        elif isinstance(obj, Driver):
            self.drivers[obj.driver_id] = obj
        elif isinstance(obj, Vehicle):
            self.vehicles[obj.vehicle_id] = obj
        else:
            raise TypeError(f"未知主数据类型: {type(obj)!r}")

    # -- 供给状态变化（由 SUPPLY_CHANGED / SCHEDULE_CHANGED 驱动） --------

    def set_trip_suspended(self, trip_id: str, suspended: bool) -> None:
        old = self.trips[trip_id]
        self.trips[trip_id] = TransitTrip(
            old.trip_id, old.route_name, old.from_node, old.to_node,
            old.departs_at, old.arrives_at, old.seats, old.wheelchair_spaces,
            old.equipment_allowed, suspended,
        )

    def set_window_closed(self, window_id: str, closed: bool) -> None:
        old = self.windows[window_id]
        self.windows[window_id] = SecurityWindow(
            old.window_id, old.venue_id, old.opens_at, old.closes_at,
            old.slots, old.accessible, closed,
        )

    def set_vehicle_offline(self, vehicle_id: str, offline: bool) -> None:
        old = self.vehicles[vehicle_id]
        self.vehicles[vehicle_id] = Vehicle(
            old.vehicle_id, old.kind, old.base_node, old.driver_id, old.seats,
            old.wheelchair_spaces, old.equipment_capacity, old.sharable,
            old.requires, offline,
        )

    def segment_is_restricted(self, segment_id: str) -> bool:
        return segment_id in self._restricted

    def set_segment_restricted(self, segment_id: str, restricted: bool) -> None:
        if restricted:
            self._restricted.add(segment_id)
        else:
            self._restricted.discard(segment_id)

    def reschedule_duty(self, duty_id: str, ready_at: datetime,
                        window_ids: tuple[str, ...] | None = None) -> None:
        old = self.duties[duty_id]
        self.duties[duty_id] = Duty(
            old.duty_id, old.venue_id, old.label, ready_at,
            old.window_ids if window_ids is None else window_ids,
        )

    # 需要在 __init__ 后声明，dataclass 不受影响

    def node_of_lodging(self, lodging_id: str) -> str:
        return self.lodgings[lodging_id].node

    def venue_of_duty(self, duty_id: str) -> Venue:
        return self.venues[self.duties[duty_id].venue_id]

    def driver_covers(self, vehicle: Vehicle) -> bool:
        if not vehicle.requires:
            return True
        driver = self.drivers.get(vehicle.driver_id or "")
        return bool(driver) and vehicle.requires <= driver.qualifications

    def clone(self) -> "Registry":
        """复制主数据与供给状态，供重排试算；试算失败不影响现状。"""
        clone = Registry()
        clone.lodgings = dict(self.lodgings)
        clone.venues = dict(self.venues)
        clone.duties = dict(self.duties)
        clone.segments = dict(self.segments)
        clone.trips = dict(self.trips)
        clone.windows = dict(self.windows)
        clone.drivers = dict(self.drivers)
        clone.vehicles = dict(self.vehicles)
        clone._restricted = set(self._restricted)
        return clone
