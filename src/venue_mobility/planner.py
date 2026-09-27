"""候选方案生成与资源占用。

规划器只回答一个问题：在当前供给与既有占用下，某份申报有哪些可行方案、
为什么可行或不可行、各占多少资源。它不写事件、不推进时钟；占用与释放由
保障服务在同一事件批次内完成，保证“多个行程争用同一资源时一次性占用”。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from .model import (
    ROLE_LABEL,
    ROLE_RANK,
    Registry,
    TravelRequest,
    TransitTrip,
    Vehicle,
)

SECURITY_BUFFER = timedelta(minutes=20)      # 到场后留出具的安检时间
SHUTTLE_JOIN_TOLERANCE = timedelta(minutes=20)  # 班车拼车的发车时差上限


@dataclass(frozen=True)
class Hold:
    """一次资源占用。resource_ref 的容量在 [start, end) 内减少 units。"""

    resource_ref: str
    kind: str                 # vehicle / seat / wheelchair / window
    start: datetime
    end: datetime
    units: int
    request_id: str
    capacity: int
    origin: str | None = None
    destination: str | None = None


@dataclass
class Reservation:
    resource_ref: str
    kind: str
    units: int
    capacity: int
    start: datetime
    end: datetime
    origin: str | None = None
    destination: str | None = None

    def as_hold(self, request_id: str) -> Hold:
        return Hold(self.resource_ref, self.kind, self.start, self.end,
                    self.units, request_id, self.capacity,
                    self.origin, self.destination)


@dataclass
class Option:
    option_id: str
    mode: str                  # dedicated 专车 / shuttle 班车 / transit 公共交通
    label: str
    depart_at: datetime
    arrive_at: datetime
    cost: int
    reservations: list[Reservation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)       # 采纳/正面理由
    rejections: list[str] = field(default_factory=list)    # 否决原因
    feasible: bool = False

    def resource_refs(self) -> list[str]:
        return [r.resource_ref for r in self.reservations]


class ResourceInventory:
    """区间容量台账：同一资源同一时段容量用尽则占用失败。"""

    def __init__(self) -> None:
        self._holds: list[Hold] = []

    # -- 查询 -------------------------------------------------------------

    def used(self, ref: str, start: datetime, end: datetime) -> int:
        return sum(h.units for h in self._holds
                   if h.resource_ref == ref and h.end > start and h.start < end)

    def holds(self, request_id: str | None = None) -> list[Hold]:
        if request_id is None:
            return list(self._holds)
        return [h for h in self._holds if h.request_id == request_id]

    def conflicting_routes(self, vehicle_id: str, start: datetime,
                           end: datetime, origin: str, destination: str) -> bool:
        """拼班车上已有同时段但起讫不同的占用，则不能再拼。"""
        prefix = f"vehicle:{vehicle_id}:seat"
        for h in self._holds:
            if (h.resource_ref == prefix and h.end > start and h.start < end
                    and (h.origin != origin or h.destination != destination)):
                return True
        return False

    # -- 变更 -------------------------------------------------------------

    def acquire(self, holds: Iterable[Hold]) -> bool:
        """整批原子占用：任一资源容量不足则全部不生效。"""
        holds = list(holds)
        capacities = {h.resource_ref: h.capacity for h in holds}
        for hold in holds:
            batch_units = sum(
                h.units for h in holds
                if h.resource_ref == hold.resource_ref
                and h.end > hold.start and h.start < hold.end
            )
            if (self.used(hold.resource_ref, hold.start, hold.end)
                    + batch_units > capacities[hold.resource_ref]):
                return False
        for hold in holds:
            self._holds.append(hold)
        return True

    def release(self, request_id: str, refs: Iterable[str] | None = None) -> list[Hold]:
        refs_set = set(refs) if refs is not None else None
        removed = [
            h for h in self._holds
            if h.request_id == request_id and (refs_set is None or h.resource_ref in refs_set)
        ]
        self._holds = [
            h for h in self._holds
            if not (h.request_id == request_id and (refs_set is None or h.resource_ref in refs_set))
        ]
        return removed

    def snapshot(self) -> "ResourceInventory":
        return ResourceInventory.from_holds(self._holds)

    @classmethod
    def from_holds(cls, holds: Iterable[Hold]) -> "ResourceInventory":
        inventory = cls()
        inventory._holds = list(holds)
        return inventory


def shortest_path(registry: Registry, src: str, dst: str) -> int | None:
    """道路最短路（分钟），避开管制路段；不可达返回 None。"""
    if src == dst:
        return 0
    adjacency: dict[str, list[tuple[str, int]]] = {}
    for seg in registry.segments.values():
        if registry.segment_is_restricted(seg.segment_id):
            continue
        adjacency.setdefault(seg.from_node, []).append((seg.to_node, seg.minutes))
        adjacency.setdefault(seg.to_node, []).append((seg.from_node, seg.minutes))
    queue: list[tuple[int, str]] = [(0, src)]
    best = {src: 0}
    while queue:
        spent, node = heapq.heappop(queue)
        if spent != best.get(node):
            continue
        if node == dst:
            return spent
        for nxt, minutes in adjacency.get(node, ()):  # type: ignore[arg-type]
            cand = spent + minutes
            if cand < best.get(nxt, 10**9):
                best[nxt] = cand
                heapq.heappush(queue, (cand, nxt))
    return None


@dataclass
class PlanResult:
    request_id: str
    chosen: Option | None
    alternatives: list[Option]
    escalation_reasons: list[str]
    rejected: list[Option] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return self.chosen is None

    def option(self, option_id: str) -> Option | None:
        pool = [self.chosen, *self.alternatives, *self.rejected]
        return next((o for o in pool if o is not None and o.option_id == option_id), None)


class Planner:
    def __init__(self, registry: Registry, inventory: ResourceInventory,
                 security_buffer: timedelta = SECURITY_BUFFER) -> None:
        self.registry = registry
        self.inventory = inventory
        self.security_buffer = security_buffer

    # -- 主入口 -----------------------------------------------------------

    def plan(self, request: TravelRequest, *, now: datetime) -> PlanResult:
        duty = self.registry.duties[request.duty_id]
        deadline = min(request.required_arrival, duty.ready_at) - self.security_buffer
        origin = self.registry.node_of_lodging(request.origin_lodging_id)
        venue = self.registry.venue_of_duty(request.duty_id)
        options: list[Option] = []
        options.extend(self._transit_options(request, origin, venue.node, deadline))
        options.extend(self._vehicle_options(request, origin, venue.node, deadline))
        feasible = [o for o in options if o.feasible]
        rejected = [o for o in options if not o.feasible]
        feasible.sort(key=lambda o: (o.cost, o.arrive_at))
        if feasible:
            chosen = feasible[0]
            chosen.reasons.append(
                f"在全部 {len(options)} 个候选中成本最低（成本分 {chosen.cost}），"
                f"且能于 {chosen.arrive_at:%H:%M} 前到场"
            )
            return PlanResult(request.request_id, chosen, feasible[1:], [],
                              rejected=rejected)
        reasons = sorted({r for o in options for r in o.rejections})
        if not reasons:
            reasons.append("当前供给中不存在任何连通驻地与场馆的候选方式")
        hard = []
        if request.medical:
            hard.append("医疗随护为硬需求，公共交通不可承担，必须使用具医疗随护资质的车辆")
        if request.wheelchair_count:
            hard.append(f"无障碍硬需求：{request.wheelchair_count} 个轮椅位，普通优先级不得覆盖")
        return PlanResult(request.request_id, None, [], hard + reasons,
                          rejected=rejected)

    # -- 公共交通 ---------------------------------------------------------

    def _transit_options(self, request: TravelRequest, origin: str,
                         venue_node: str, deadline: datetime) -> list[Option]:
        options: list[Option]
        options = []
        for trip in self.registry.trips.values():
            label = f"公交 {trip.route_name}（{trip.trip_id}）"
            option = Option(
                option_id=f"transit-{trip.trip_id}",
                mode="transit", label=label,
                depart_at=trip.departs_at, arrive_at=trip.arrives_at, cost=50,
            )
            checks = self._check_transit(request, trip, origin, venue_node, deadline)
            option.feasible = not checks
            option.rejections = checks
            if not checks:
                seat_units = request.party_size + request.equipment_units
                reservations = [
                    Reservation(f"transit:{trip.trip_id}:seat", "seat",
                                seat_units, trip.seats, trip.departs_at, trip.arrives_at,
                                origin, venue_node),
                ]
                if request.wheelchair_count:
                    reservations.append(Reservation(
                        f"transit:{trip.trip_id}:wheelchair", "wheelchair",
                        request.wheelchair_count, trip.wheelchair_spaces,
                        trip.departs_at, trip.arrives_at, origin, venue_node))
                window = self._pick_window(request, trip.arrives_at, option.rejections)
                if window is None:
                    option.feasible = False
                else:
                    reservations.append(Reservation(
                        f"window:{window.window_id}", "window", request.party_size,
                        window.slots, window.opens_at, window.closes_at))
                    option.reservations = reservations
                    remaining = trip.seats - self.inventory.used(
                        f"transit:{trip.trip_id}:seat", trip.departs_at, trip.arrives_at)
                    option.reasons.append(
                        f"{label} {trip.departs_at:%H:%M} 发车、{trip.arrives_at:%H:%M} 抵达，"
                        f"当前余票 {remaining} 张，本次占用 {seat_units} 张"
                    )
            options.append(option)
        return options

    def _check_transit(self, request: TravelRequest, trip: TransitTrip, origin: str,
                       venue_node: str, deadline: datetime) -> list[str]:
        fails: list[str] = []
        if trip.suspended:
            fails.append(f"班次 {trip.trip_id} 已停运")
        if trip.from_node != origin or trip.to_node != venue_node:
            fails.append(f"班次 {trip.trip_id} 起止站不匹配驻地到场馆")
        if trip.arrives_at > deadline:
            fails.append(f"班次 {trip.trip_id} 抵达 {trip.arrives_at:%H:%M} 晚于最晚到场 {deadline:%H:%M}")
        seat_units = request.party_size + request.equipment_units
        if seat_units > trip.seats:
            fails.append(f"班次 {trip.trip_id} 座位不足（需 {seat_units}，共 {trip.seats}）")
        elif self.inventory.used(f"transit:{trip.trip_id}:seat",
                                 trip.departs_at, trip.arrives_at) + seat_units > trip.seats:
            fails.append(f"班次 {trip.trip_id} 余票已被其他行程订满")
        if request.wheelchair_count:
            ref = f"transit:{trip.trip_id}:wheelchair"
            if trip.wheelchair_spaces < request.wheelchair_count:
                fails.append(f"班次 {trip.trip_id} 轮椅位不足")
            elif self.inventory.used(ref, trip.departs_at, trip.arrives_at) + request.wheelchair_count > trip.wheelchair_spaces:
                fails.append(f"班次 {trip.trip_id} 轮椅位已约满")
        if request.equipment_units and not trip.equipment_allowed:
            fails.append(f"班次 {trip.trip_id} 不允许器材同行（{request.equipment_units} 件）")
        if request.medical:
            fails.append("医疗随护需求不能使用公共交通")
        return fails

    # -- 车辆（专车/班车） ------------------------------------------------

    def _vehicle_options(self, request: TravelRequest, origin: str,
                         venue_node: str, deadline: datetime) -> list[Option]:
        options: list[Option] = []
        leg = shortest_path(self.registry, origin, venue_node)
        if leg is None:
            dead = Option("vehicle-unreachable", "dedicated", "道路不可达",
                          deadline, deadline, 10**9)
            dead.rejections.append("驻地到场馆的道路全部处于管制状态，车辆无法通行")
            return [dead]
        leg_delta = timedelta(minutes=leg)
        for vehicle in self.registry.vehicles.values():
            option = self._check_vehicle(request, vehicle, origin, venue_node,
                                         leg, leg_delta, deadline)
            options.append(option)
        return options

    def _check_vehicle(self, request: TravelRequest, vehicle: Vehicle, origin: str,
                       venue_node: str, leg: int, leg_delta: timedelta,
                       deadline: datetime) -> Option:
        mode = "shuttle" if vehicle.sharable else "dedicated"
        label = f"{'班车' if vehicle.sharable else '专车'} {vehicle.vehicle_id}"
        deadhead = shortest_path(self.registry, vehicle.base_node, origin)
        depart = deadline - leg_delta
        arrive = depart + leg_delta
        option = Option(
            option_id=f"{mode}-{vehicle.vehicle_id}",
            mode=mode, label=label, depart_at=depart, arrive_at=arrive,
            cost=200 + leg if vehicle.sharable else 500 + leg,
        )
        fails = option.rejections
        if vehicle.offline:
            fails.append(f"车辆 {vehicle.vehicle_id} 已下线")
        if deadhead is None:
            fails.append(f"车辆 {vehicle.vehicle_id} 驻地到出发点道路受管制，无法空驶接人")
        driver = self.registry.drivers.get(vehicle.driver_id or "")
        if not driver:
            fails.append(f"车辆 {vehicle.vehicle_id} 未配备有效司机")
        elif not self.registry.driver_covers(vehicle):
            have = "、".join(sorted(driver.qualifications))
            fails.append(
                f"车辆 {vehicle.vehicle_id} 司机资质不足（需要 {sorted(vehicle.requires)}，现有 {have}）"
            )
        if request.medical:
            if not driver or "medical" not in driver.qualifications:
                fails.append(f"车辆 {vehicle.vehicle_id} 司机不具备医疗随护资质，医疗硬需求不可降级")
        # 器材先进器材舱，超出部分占用座位
        equipment_overflow = max(0, request.equipment_units - vehicle.equipment_capacity)
        seat_need = request.party_size + equipment_overflow
        if vehicle.wheelchair_spaces < request.wheelchair_count:
            fails.append(f"车辆 {vehicle.vehicle_id} 轮椅位不足（需 {request.wheelchair_count}）")
        if vehicle.seats < seat_need:
            bits = f"{request.party_size} 人"
            if equipment_overflow:
                bits += f"加 {equipment_overflow} 件器材占座"
            fails.append(f"车辆 {vehicle.vehicle_id} 座位不足（需 {bits}，共 {vehicle.seats} 座）")
        start = depart - timedelta(minutes=deadhead or 0)
        reservations: list[Reservation] = []
        if not vehicle.sharable:
            ref = f"vehicle:{vehicle.vehicle_id}"
            if self.inventory.used(ref, start, arrive) > 0:
                fails.append(f"专车 {vehicle.vehicle_id} 该时段已被其他行程整包占用")
            reservations.append(Reservation(ref, "vehicle", 1, 1, start, arrive,
                                            origin, venue_node))
        else:
            ref = f"vehicle:{vehicle.vehicle_id}:seat"
            if self.inventory.conflicting_routes(vehicle.vehicle_id, start, arrive,
                                                 origin, venue_node):
                fails.append(f"班车 {vehicle.vehicle_id} 同时段承担其他线路，无法拼乘")
            elif self.inventory.used(ref, start, arrive) + seat_need > vehicle.seats:
                fails.append(f"班车 {vehicle.vehicle_id} 该时段余座不足（需 {seat_need}）")
            else:
                reservations.append(Reservation(ref, "seat", seat_need,
                                                vehicle.seats, start, arrive,
                                                origin, venue_node))
            wref = f"vehicle:{vehicle.vehicle_id}:wheelchair"
            if request.wheelchair_count:
                if self.inventory.used(wref, start, arrive) + request.wheelchair_count > vehicle.wheelchair_spaces:
                    fails.append(f"班车 {vehicle.vehicle_id} 轮椅位已约满")
                else:
                    reservations.append(Reservation(wref, "wheelchair",
                                                    request.wheelchair_count,
                                                    vehicle.wheelchair_spaces,
                                                    start, arrive, origin, venue_node))
        window = None
        if not fails:
            window = self._pick_window(request, arrive, fails)
        if fails:
            return option
        assert window is not None
        reservations.append(Reservation(
            f"window:{window.window_id}", "window", request.party_size,
            window.slots, window.opens_at, window.closes_at))
        option.reservations = reservations
        option.feasible = True
        role = ROLE_LABEL[request.role]
        need_bits = []
        if request.medical:
            need_bits.append("医疗随护")
        if request.wheelchair_count:
            need_bits.append(f"{request.wheelchair_count} 轮椅位")
        if request.equipment_units:
            need_bits.append(f"{request.equipment_units} 件器材")
        need_text = "（含" + "、".join(need_bits) + "）" if need_bits else ""
        option.reasons.append(
            f"{label} 可满足{role}{need_text} {request.party_size} 人，"
            f"{depart:%H:%M} 出发、{arrive:%H:%M} 抵达，道路 {leg} 分钟"
            + (f"，空驶接人 {deadhead} 分钟" if deadhead else "")
        )
        return option

    # -- 安检窗口 ---------------------------------------------------------

    def _pick_window(self, request: TravelRequest, arrive_at: datetime,
                     fails: list[str]):
        duty = self.registry.duties[request.duty_id]
        candidates = []
        for window in self.registry.windows.values():
            if window.venue_id != duty.venue_id:
                continue
            if duty.window_ids and window.window_id not in duty.window_ids:
                continue
            if not (window.opens_at <= arrive_at <= window.closes_at):
                continue
            candidates.append(window)
        if not candidates:
            fails.append("抵达时刻没有开放的安检窗口")
            return None
        accessible_needed = request.wheelchair_count > 0 or request.medical
        usable = [w for w in candidates if w.accessible or not accessible_needed]
        if not usable:
            fails.append("仅剩非无障碍安检窗口，无法满足医疗/无障碍通行")
            return None
        open_windows = [w for w in sorted(usable, key=lambda w: w.opens_at)
                        if not w.closed]
        if not open_windows:
            kind = "无障碍" if accessible_needed else ""
            fails.append(f"抵达时刻匹配的{kind}安检窗口均已临时关闭")
            return None
        for window in open_windows:
            ref = f"window:{window.window_id}"
            used = self.inventory.used(ref, window.opens_at, window.closes_at)
            if used + request.party_size <= window.slots:
                return window
            fails.append(f"安检窗口 {window.window_id} 时段容量已满（{used}/{window.slots}）")
        return None


def planning_order_key(request: TravelRequest) -> tuple:
    """资源争用裁决顺序：硬需求先行（普通优先级不得覆盖），再角色优先级，
    最后就绪早者优先。"""
    return (
        0 if request.hard_need else 1,
        -ROLE_RANK[request.role],
        request.required_arrival,
        request.request_id,
    )
