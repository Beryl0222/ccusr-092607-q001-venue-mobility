"""出发方案规划：把静态事实转成可解释的候选路径。

规划器不做在途占用判断，只回答两个问题：

1. 对一段申报，哪些路径在硬约束下可行（资质、无障碍、医疗、器材、管制、停运、安检窗口、角色余量）；
2. 可行路径按成本排序，主方案最便宜，其余为备用路径。

争用容量（座位、安检名额）由服务层在占用时一次性判定；容量不足时服务层会带上
``exclude_refs`` 重新规划，规划器不感知是谁占用了资源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import times
from .model import (
    ATHLETE,
    MEDIA,
    MODE_TRANSIT,
    MODE_VEHICLE,
    NEED_ACCESSIBLE,
    NEED_MEDICAL,
    STAFF,
    RequestInput,
    Scenario,
)

# 不同角色在安检完成后到开赛之间必须保留的准备余量（分钟）
ROLE_PREP_MARGIN = {ATHLETE: 30, MEDIA: 15, STAFF: 0}

TRANSIT_COST_PER_PERSON = 8
VEHICLE_DISPATCH_COST = 120
VEHICLE_COST_PER_PERSON = 15


@dataclass(frozen=True)
class TravelOption:
    """一条可执行路径：交通资源 + 安检窗口一次性成套。"""

    option_id: str
    mode: str
    resource_ref: str
    resource_type: str  # transit_seat / vehicle
    security_slot_id: str
    departs_at: str
    arrives_at: str  # 抵达场馆安检外
    cleared_at: str  # 安检完成、抵达备赛区
    cost: int
    seats_required: int
    why: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "option_id": self.option_id,
            "mode": self.mode,
            "resource_ref": self.resource_ref,
            "resource_type": self.resource_type,
            "security_slot_id": self.security_slot_id,
            "departs_at": self.departs_at,
            "arrives_at": self.arrives_at,
            "cleared_at": self.cleared_at,
            "cost": self.cost,
            "seats_required": self.seats_required,
            "why": list(self.why),
        }


@dataclass(frozen=True)
class Rejection:
    resource_ref: str
    mode: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    business_ref: str
    feasible: bool
    primary: TravelOption | None
    backups: tuple[TravelOption, ...]
    rejections: tuple[Rejection, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "business_ref": self.business_ref,
            "feasible": self.feasible,
            "primary": self.primary.to_dict() if self.primary else None,
            "backups": [option.to_dict() for option in self.backups],
            "rejections": [
                {"resource_ref": item.resource_ref, "mode": item.mode, "reasons": list(item.reasons)}
                for item in self.rejections
            ],
            "reasons": list(self.reasons),
        }


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return times.parse(start_a) < times.parse(end_b) and times.parse(start_b) < times.parse(end_a)


def plan_request(request: RequestInput, scenario: Scenario, exclude_refs: frozenset[str] = frozenset()) -> Plan:
    session = scenario.session(request.session_id)
    residence = scenario.residence(request.residence_id)
    base_reasons: list[str] = []
    if session is None:
        return Plan(request.business_ref, False, None, (), (), ("session_unknown",))
    if residence is None or residence.venue_id != session.venue_id:
        base_reasons.append("residence_not_serving_venue")
        return Plan(request.business_ref, False, None, (), (), tuple(base_reasons))
    if request.role not in ROLE_PREP_MARGIN:
        return Plan(request.business_ref, False, None, (), (), ("role_unknown",))

    deadline = times.parse(session.latest_arrival_at)
    margin = ROLE_PREP_MARGIN[request.role]
    candidates: list[TravelOption] = []
    rejections: list[Rejection] = []

    slots = [
        slot
        for slot in scenario.security_slots
        if slot.venue_id == session.venue_id and slot.security_zone == session.security_zone
        and slot.capacity >= request.party_size
    ]

    def slot_fits(cleared: str) -> bool:
        return (
            times.parse(cleared) <= deadline
            and times.parse(cleared) <= times.parse(times.minutes_after(session.start_at, -margin))
        )

    # 公共交通：固定班次，准点乘降
    for transit in scenario.transit:
        if transit.residence_id != residence.residence_id or transit.venue_id != session.venue_id:
            continue
        reasons: list[str] = []
        if transit.suspended:
            reasons.append("transit_suspended")
        if request.need_tier in (NEED_ACCESSIBLE, NEED_MEDICAL):
            reasons.append("transit_not_accessible_graded")
        if request.equipment_units > 0:
            reasons.append("equipment_requires_vehicle")
        slot = next(
            (
                slot
                for slot in slots
                if times.parse(slot.starts_at) >= times.parse(transit.arrives_at)
                and slot_fits(slot.ends_at)
            ),
            None,
        )
        if slot is None and not reasons:
            reasons.append("no_security_slot")
        if transit.seats < request.party_size:
            reasons.append("transit_undersized")
        if reasons:
            rejections.append(Rejection(transit.transit_id, MODE_TRANSIT, tuple(reasons)))
            continue
        if transit.transit_id in exclude_refs:
            continue
        candidates.append(
            TravelOption(
                option_id=f"{request.business_ref}:{MODE_TRANSIT}:{transit.transit_id}",
                mode=MODE_TRANSIT,
                resource_ref=transit.transit_id,
                resource_type="transit_seat",
                security_slot_id=slot.slot_id,
                departs_at=transit.departs_at,
                arrives_at=transit.arrives_at,
                cleared_at=slot.ends_at,
                cost=TRANSIT_COST_PER_PERSON * request.party_size,
                seats_required=request.party_size,
                why=("公共交通准点可达", "成本最低"),
            )
        )

    # 专车：出发时刻按所选安检窗口倒推
    for vehicle in scenario.vehicles:
        reasons: list[str] = []
        if vehicle.based_at_residence is not None and vehicle.based_at_residence != residence.residence_id:
            reasons.append("vehicle_based_elsewhere")
        if vehicle.seats < request.party_size:
            reasons.append("vehicle_undersized")
        if request.equipment_units > 0 and vehicle.equipment_capacity < request.equipment_units:
            reasons.append("equipment_capacity_insufficient")
        if request.need_tier in (NEED_ACCESSIBLE, NEED_MEDICAL) and not vehicle.accessible:
            reasons.append("vehicle_not_accessible")
        if request.need_tier == NEED_MEDICAL and not vehicle.medical_graded:
            reasons.append("vehicle_not_medical_graded")
        if session.security_zone not in vehicle.credential_zones:
            reasons.append("zone_credential_missing")

        picked_slot = None
        departs_at = ""
        for slot in slots:
            candidate_departs = times.minutes_after(slot.starts_at, -residence.vehicle_minutes)
            candidate_arrive = slot.starts_at
            closed = any(
                closure.residence_id == residence.residence_id
                and closure.venue_id == session.venue_id
                and _overlaps(candidate_departs, candidate_arrive, closure.starts_at, closure.ends_at)
                for closure in scenario.closures
            )
            if closed:
                continue
            if slot_fits(slot.ends_at):
                picked_slot = slot
                departs_at = candidate_departs
                break
        if picked_slot is None and not reasons:
            reasons.append("no_security_slot_or_route_closed")

        if reasons:
            rejections.append(Rejection(vehicle.vehicle_id, MODE_VEHICLE, tuple(reasons)))
            continue
        if vehicle.vehicle_id in exclude_refs:
            continue
        why = ["专车门到安检", "出发时刻随安检窗口倒推"]
        if request.need_tier == NEED_MEDICAL:
            why.append("医疗分级车辆")
        elif request.need_tier == NEED_ACCESSIBLE:
            why.append("无障碍车辆")
        candidates.append(
            TravelOption(
                option_id=f"{request.business_ref}:{MODE_VEHICLE}:{vehicle.vehicle_id}",
                mode=MODE_VEHICLE,
                resource_ref=vehicle.vehicle_id,
                resource_type="vehicle",
                security_slot_id=picked_slot.slot_id,
                departs_at=departs_at,
                arrives_at=picked_slot.starts_at,
                cleared_at=picked_slot.ends_at,
                cost=VEHICLE_DISPATCH_COST + VEHICLE_COST_PER_PERSON * request.party_size,
                seats_required=request.party_size,
                why=tuple(why),
            )
        )

    candidates.sort(key=lambda option: (option.cost, times.parse(option.departs_at), option.resource_ref))
    if not candidates:
        return Plan(
            request.business_ref,
            False,
            None,
            (),
            tuple(sorted(rejections, key=lambda item: item.resource_ref)),
            ("no_feasible_option",),
        )
    primary = candidates[0]
    return Plan(
        request.business_ref,
        True,
        primary,
        tuple(candidates[1:]),
        tuple(sorted(rejections, key=lambda item: item.resource_ref)),
        (),
    )
