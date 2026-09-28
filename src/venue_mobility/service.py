"""分散赛区通勤保障决策服务。

所有状态都由事件流导出（事件溯源）：

* 申报经 ``REQUEST_SUBMITTED / REQUEST_ACCEPTED`` 登记；同业务编号指纹不变为幂等，
  人员、时间或路线变化进入核查（``REQUEST_ESCALATED_REVIEW``）。
* 方案经 ``OPTION_PROPOSED`` 给出；确认时把车辆/座位/安检名额作为同一事务
  ``RESOURCE_RESERVED + DISPATCH_CONFIRMED`` 一次性占用，要么全部成功要么不留痕。
* 争用时按优先级抢占：医疗与无障碍保护档不会被普通角色优先级覆盖；
  已经发车（时钟越过出发时刻）或已完成的转运不可抢占。
* 停运、管制、改期只重排真正受影响的在途行程；完成的转运随 ``COMMITMENT_CLOSED``
  保留当时依据，事件历史永不改写。
* 确认、候车、失约、升级由可控时钟推进；从事件流重建即可恢复全部未结义务。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import times
from .clock import ControlledClock
from .model import (
    ATHLETE,
    MEDIA,
    NEED_ACCESSIBLE,
    NEED_MEDICAL,
    STAFF,
    RequestInput,
    Scenario,
)
from .planner import Plan, plan_request

# 普通角色优先级（仅在同档之间比较；不能跨越医疗/无障碍保护档）
ROLE_RANK = {ATHLETE: 3, MEDIA: 2, STAFF: 1}

STATUS_SUBMITTED = "submitted"
STATUS_REVIEWING = "reviewing"
STATUS_PLANNED = "planned"
STATUS_DISPATCHED = "dispatched"
STATUS_WAITING = "waiting"  # 已派车并在登乘点候车，资源仍占用
STATUS_AWAITING_UPGRADE = "awaiting_upgrade"
STATUS_STRANDED = "stranded"  # 中断后暂无可行路径，保障义务仍挂起
STATUS_COMPLETED = "completed"
STATUS_CLOSED = "closed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

ACTIVE_STATUSES = {
    STATUS_PLANNED,
    STATUS_WAITING,
    STATUS_DISPATCHED,
    STATUS_AWAITING_UPGRADE,
}
DISPATCHED_STATUSES = {STATUS_DISPATCHED, STATUS_WAITING}
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_CLOSED, STATUS_REJECTED}

WAIT_MINUTES_DEFAULT = 15
UPGRADE_GRACE_MINUTES = 20
MAX_PREEMPTION_DEPTH = 6


class ServiceError(ValueError):
    """业务规则拒绝本次命令。"""


@dataclass
class Proposal:
    nonce: int
    option: dict[str, str]
    reason: str


@dataclass
class JourneyState:
    business_ref: str
    request: RequestInput
    status: str = STATUS_SUBMITTED
    proposals: list[Proposal] = field(default_factory=list)
    nonce: int = 0
    held_atoms: list[str] = field(default_factory=list)
    basis_option: dict[str, Any] | None = None  # 已确认/已完成方案的当时依据
    expires_at: str | None = None
    upgrade_due_at: str | None = None
    review_reason: str | None = None
    notification_keys: set[str] = field(default_factory=set)

    @property
    def current(self) -> Proposal | None:
        return self.proposals[-1] if self.proposals else None


def _atoms_for(option: Mapping[str, Any]) -> list[tuple[str, int]]:
    """返回 (资源原子键, 占用单位)。车辆整车 1 单位，座位/名额按人数。"""
    units = int(option["seats_required"])
    atoms = [(f"slot:{option['security_slot_id']}", units)]
    if option["resource_type"] == "vehicle":
        atoms.append((f"vehicle:{option['resource_ref']}", 1))
    else:
        atoms.append((f"transit:{option['resource_ref']}", units))
    return atoms


class MobilityService:
    def __init__(
        self,
        scenario: Scenario,
        events: Iterable[Mapping[str, Any]] = (),
        start_at: str = "2026-09-25T06:00:00+08:00",
    ) -> None:
        self.scenario = scenario
        self.events: list[dict[str, Any]] = []
        self._versions: dict[tuple[str, str], int] = {}
        self.clock = ControlledClock(times.parse(start_at))
        self.journeys: dict[str, JourneyState] = {}
        self._notification_index: dict[str, str] = {}
        # 资源原子 -> {业务编号: 占用单位}
        self.holdings: dict[str, dict[str, int]] = {}
        self.suspended_transit: set[str] = set()
        self.closures: list[dict[str, Any]] = list()
        for event in events:
            self._apply(dict(event))
        if self.events:
            last_occurred = times.parse(self.events[-1]["occurred_at"])
            self.clock.advance_to(last_occurred)

    # ------------------------------------------------------------------ 持久化

    @classmethod
    def from_store(cls, path: str | Path, scenario: Scenario, start_at: str | None = None) -> "MobilityService":
        store = Path(path)
        events = []
        if store.exists():
            events = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line.strip()]
        return cls(scenario, events, start_at=start_at or "2026-09-25T06:00:00+08:00")

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in self.events) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ 事件落盘

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        event = {
            "event_id": f"E{len(self.events) + 1:05d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": times.iso(self.clock.now),
            "version": version,
            "payload": payload,
        }
        self.events.append(event)
        self._versions[key] = version
        self._project(event)
        return event

    def _apply(self, event: Mapping[str, Any]) -> None:
        """重放入口：去重后投影。实时提交走 ``_emit``。"""
        if event["event_id"] in {e["event_id"] for e in self.events}:
            return
        self.events.append(dict(event))
        self._versions[(event["aggregate_type"], event["aggregate_id"])] = event["version"]
        self._project(event)

    def _project(self, event: Mapping[str, Any]) -> None:
        """把单个事件投影到内存状态；重建与实时提交共用同一条路径。"""
        p = event["payload"]
        etype = event["event_type"]

        if etype in ("REQUEST_SUBMITTED", "REQUEST_ESCALATED_REVIEW"):
            req = _request_from_payload(p["request"])
            state = self.journeys.get(req.business_ref)
            if state is None:
                state = JourneyState(business_ref=req.business_ref, request=req)
                self.journeys[req.business_ref] = state
            else:
                state.request = req
                if state.status in (STATUS_REJECTED, STATUS_EXPIRED, STATUS_STRANDED):
                    # 同一业务编号重新申报：清理上一轮方案痕迹。
                    state.status = STATUS_SUBMITTED
                    state.proposals = []
                    state.nonce = 0
                    state.basis_option = None
                    state.held_atoms = []
                    state.expires_at = None
                    state.upgrade_due_at = None
                    state.review_reason = None
            for key in p.get("notification_keys", ()):
                state.notification_keys.add(key)
                self._notification_index.setdefault(key, req.business_ref)
            if etype == "REQUEST_ESCALATED_REVIEW":
                state.status = STATUS_REVIEWING
                state.review_reason = p.get("reason")
        elif etype == "REQUEST_ACCEPTED":
            state = self.journeys[p["business_ref"]]
            if state.status in (STATUS_SUBMITTED, STATUS_REVIEWING):
                state.status = STATUS_PLANNED
        elif etype == "OPTION_PROPOSED":
            state = self.journeys[p["business_ref"]]
            state.nonce += 1
            state.proposals.append(Proposal(state.nonce, p["option"], p.get("reason", "primary")))
            if p.get("upgrade_due_at"):
                state.status = STATUS_AWAITING_UPGRADE
                state.upgrade_due_at = p["upgrade_due_at"]
            elif state.status not in (STATUS_WAITING, STATUS_DISPATCHED):
                state.status = STATUS_PLANNED
                state.upgrade_due_at = None
        elif etype == "WAITING_STARTED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_WAITING
            state.expires_at = p["expires_at"]
        elif etype in ("RESOURCE_RESERVED", "RESOURCE_RELEASED"):
            holder_state = self.journeys.get(p["business_ref"])
            for atom in p["atoms"]:
                key = atom["key"]
                units = int(atom["units"])
                bucket = self.holdings.setdefault(key, {})
                if etype == "RESOURCE_RESERVED":
                    bucket[p["business_ref"]] = bucket.get(p["business_ref"], 0) + units
                else:
                    bucket[p["business_ref"]] = bucket.get(p["business_ref"], 0) - units
                    if bucket[p["business_ref"]] <= 0:
                        bucket.pop(p["business_ref"], None)
            if etype == "RESOURCE_RELEASED" and holder_state is not None:
                holder_state.held_atoms = []
        elif etype == "DISPATCH_CONFIRMED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_DISPATCHED
            state.basis_option = p["option"]
            state.held_atoms = [atom["key"] for atom in p["atoms"]]
            state.expires_at = None
        elif etype == "WAITING_EXPIRED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_EXPIRED
            state.expires_at = None
        elif etype == "JOURNEY_REPLANNED":
            # 释放投影由 RESOURCE_RELEASED 完成；这里仅作为状态迁移标记。
            state = self.journeys[p["business_ref"]]
        elif etype == "JOURNEY_UPGRADED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_DISPATCHED
            state.basis_option = p["option"]
            state.held_atoms = [atom["key"] for atom in p["atoms"]]
            state.upgrade_due_at = None
        elif etype == "TRANSFER_COMPLETED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_COMPLETED
            state.held_atoms = []
        elif etype == "COMMITMENT_CLOSED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_CLOSED
            state.held_atoms = []
        elif etype == "REQUEST_REJECTED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_REJECTED
        elif etype == "JOURNEY_STRANDED":
            state = self.journeys[p["business_ref"]]
            state.status = STATUS_STRANDED
            state.held_atoms = []
            state.review_reason = p.get("reason")
        elif etype == "DISRUPTION_DECLARED":
            for ref in p.get("suspended_transit", ()):
                self.suspended_transit.add(ref)
            for closure in p.get("closures", ()):
                self.closures.append(closure)
            if p.get("session_id"):
                self._apply_session_change(p["session_id"], p["start_at"], p["latest_arrival_at"])
        elif etype == "DISRUPTION_CLEARED":
            lifted = set(p.get("closures_lifted", ()))
            if lifted:
                self.closures = [item for item in self.closures if item["closure_id"] not in lifted]
            for ref in p.get("transit_restored", ()):
                self.suspended_transit.discard(ref)

    # ------------------------------------------------------------------ 申报

    def submit_request(self, data: Mapping[str, Any]) -> dict[str, Any]:
        request = _request_from_payload(data)
        state = self.journeys.get(request.business_ref)

        if request.notification_key:
            owner = self._notification_index.get(request.notification_key)
            if owner is not None and owner != request.business_ref:
                raise ServiceError("通知幂等键已被其他业务编号占用")
            if state is not None and request.notification_key in state.notification_keys:
                return {"action": "duplicate", "business_ref": request.business_ref, "status": state.status}

        fingerprint = _fingerprint(request)
        if state is not None and state.status in (STATUS_REVIEWING,):
            raise ServiceError("业务编号仍在核查中")
        if state is not None and state.status not in (STATUS_REJECTED, STATUS_EXPIRED, STATUS_STRANDED):
            if _fingerprint(state.request) == fingerprint:
                if request.notification_key:
                    state.notification_keys.add(request.notification_key)
                    self._notification_index[request.notification_key] = request.business_ref
                return {"action": "duplicate", "business_ref": request.business_ref, "status": state.status}
            self._emit(
                "REQUEST_SUBMITTED",
                "travel_request",
                request.business_ref,
                {"request": dict(data), "notification_keys": _keys(request)},
            )
            self._emit(
                "REQUEST_ESCALATED_REVIEW",
                "travel_request",
                request.business_ref,
                {"reason": "business_ref_conflict", "request": dict(data)},
            )
            return {"action": "review", "business_ref": request.business_ref, "reason": "business_ref_conflict"}

        self._emit(
            "REQUEST_SUBMITTED",
            "travel_request",
            request.business_ref,
            {"request": dict(data), "notification_keys": _keys(request)},
        )
        if request.notification_key:
            self._notification_index[request.notification_key] = request.business_ref
        self._emit(
            "REQUEST_ACCEPTED",
            "travel_request",
            request.business_ref,
            {
                "business_ref": request.business_ref,
                "traveler_role": request.role,
                "required_arrival": self._required_arrival(request),
            },
        )
        plan = self._plan(request)
        if not plan.feasible:
            self._emit(
                "REQUEST_REJECTED",
                "travel_request",
                request.business_ref,
                {"business_ref": request.business_ref, "reasons": list(plan.reasons)},
            )
            return {"action": "rejected", "business_ref": request.business_ref, "plan": plan.to_dict()}
        self._propose(plan, reason="primary")
        return {"action": "planned", "business_ref": request.business_ref, "plan": plan.to_dict()}

    def resolve_review(self, business_ref: str, accept: bool) -> dict[str, Any]:
        state = self._state(business_ref)
        if state.status != STATUS_REVIEWING:
            raise ServiceError("业务编号不在核查中")
        if not accept:
            self._emit(
                "REQUEST_REJECTED",
                "travel_request",
                business_ref,
                {"business_ref": business_ref, "reasons": ["review_declined"]},
            )
            return {"action": "rejected", "business_ref": business_ref}
        self._emit(
            "REQUEST_ACCEPTED",
            "travel_request",
            business_ref,
            {
                "business_ref": business_ref,
                "traveler_role": state.request.role,
                "required_arrival": self._required_arrival(state.request),
            },
        )
        plan = self._plan(state.request)
        if not plan.feasible:
            self._emit(
                "REQUEST_REJECTED",
                "travel_request",
                business_ref,
                {"business_ref": business_ref, "reasons": list(plan.reasons)},
            )
            return {"action": "rejected", "business_ref": business_ref, "plan": plan.to_dict()}
        self._propose(plan, reason="after_review")
        return {"action": "planned", "business_ref": business_ref, "plan": plan.to_dict()}

    # ------------------------------------------------------------------ 确认/候车

    def confirm(self, business_ref: str, option_id: str | None = None) -> dict[str, Any]:
        state = self._state(business_ref, {STATUS_PLANNED, STATUS_AWAITING_UPGRADE})
        proposal = state.current
        if proposal is None:
            raise ServiceError("没有可确认的方案")
        option = self._select_option(state.request, option_id) if option_id else self._choose_option(state.request)
        if state.status == STATUS_AWAITING_UPGRADE:
            return self._accept_upgrade(state, option)
        self._reserve(state, option, reason="confirmed", event_type="DISPATCH_CONFIRMED", allow_preempt=True)
        return {"action": "dispatched", "business_ref": business_ref, "option": option}

    def begin_waiting(self, business_ref: str, minutes: int = WAIT_MINUTES_DEFAULT) -> dict[str, Any]:
        state = self._state(business_ref, {STATUS_DISPATCHED})
        if minutes <= 0:
            raise ServiceError("候车时长必须为正")
        expires_at = times.minutes_after(times.iso(self.clock.now), minutes)
        self._emit(
            "WAITING_STARTED",
            "journey_commitment",
            business_ref,
            {"business_ref": business_ref, "expires_at": expires_at, "wait_minutes": minutes},
        )
        return {"action": "waiting", "business_ref": business_ref, "expires_at": expires_at}

    def complete_transfer(self, business_ref: str) -> dict[str, Any]:
        state = self._state(business_ref, DISPATCHED_STATUSES)
        option = state.basis_option
        assert option is not None
        self._release(state, reason="completed")
        self._emit(
            "TRANSFER_COMPLETED",
            "journey_commitment",
            business_ref,
            {"business_ref": business_ref, "option": option, "basis_nonce": state.nonce},
        )
        self._emit(
            "COMMITMENT_CLOSED",
            "journey_commitment",
            business_ref,
            {
                "business_ref": business_ref,
                "outcome": "completed",
                "option": option,
                "basis_nonce": state.nonce,
            },
        )
        return {"action": "closed", "business_ref": business_ref, "outcome": "completed"}

    # ------------------------------------------------------------------ 时钟

    def advance(self, minutes: int) -> dict[str, Any]:
        self.clock.advance(minutes=minutes)
        return self.tick()

    def tick(self) -> dict[str, Any]:
        """推进所有到期节点：候车失约、升级自动生效。幂等，可在恢复后补跑。"""
        fired: list[dict[str, Any]] = []
        now = times.iso(self.clock.now)
        for state in list(self.journeys.values()):
            if state.status == STATUS_WAITING and state.expires_at and now >= state.expires_at:
                self._release(state, reason="no_show")
                self._emit(
                    "WAITING_EXPIRED",
                    "journey_commitment",
                    state.business_ref,
                    {"business_ref": state.business_ref, "expires_at": state.expires_at, "outcome": "no_show"},
                )
                self._emit(
                    "COMMITMENT_CLOSED",
                    "journey_commitment",
                    state.business_ref,
                    {"business_ref": state.business_ref, "outcome": "no_show", "option": state.basis_option},
                )
                fired.append({"business_ref": state.business_ref, "node": "no_show"})
            elif state.status == STATUS_AWAITING_UPGRADE and state.upgrade_due_at and now >= state.upgrade_due_at:
                proposal = state.current
                try:
                    self._accept_upgrade(state, proposal.option, automatic=True)
                    fired.append({"business_ref": state.business_ref, "node": "upgrade_auto"})
                except ServiceError as exc:
                    self._strand(state, "upgrade_blocked", str(exc))
                    fired.append({"business_ref": state.business_ref, "node": "upgrade_blocked"})
        return {"now": now, "fired": fired}

    def _strand(self, state: JourneyState, reason: str, detail: str = "") -> None:
        if state.basis_option:
            self._release(state, reason=reason)
        self._emit(
            "JOURNEY_STRANDED",
            "journey_commitment",
            state.business_ref,
            {"business_ref": state.business_ref, "reason": reason, "detail": detail, "from_option": state.basis_option},
        )

    # ------------------------------------------------------------------ 中断与重排

    def suspend_transit(self, transit_id: str, reason: str) -> dict[str, Any]:
        self._emit(
            "DISRUPTION_DECLARED",
            "venue_schedule",
            "network",
            {"reason": reason, "suspended_transit": [transit_id], "closures": []},
        )
        return self._replan_affected(reason=reason)

    def impose_closure(self, closure: Mapping[str, Any], reason: str) -> dict[str, Any]:
        self._emit(
            "DISRUPTION_DECLARED",
            "venue_schedule",
            "network",
            {"reason": reason, "suspended_transit": [], "closures": [dict(closure)]},
        )
        return self._replan_affected(reason=reason)

    def reschedule_session(self, session_id: str, start_at: str, latest_arrival_at: str, reason: str) -> dict[str, Any]:
        if self.scenario.session(session_id) is None:
            raise ServiceError("未知场次")
        self._emit(
            "DISRUPTION_DECLARED",
            "venue_schedule",
            session_id,
            {"reason": reason, "session_id": session_id, "start_at": start_at, "latest_arrival_at": latest_arrival_at},
        )
        return self._replan_affected(reason=reason)

    def lift_closure(self, closure_id: str, reason: str = "route_reopened") -> dict[str, Any]:
        if not any(item.get("closure_id") == closure_id for item in self.closures):
            raise ServiceError("没有这条运行态管制")
        self._emit(
            "DISRUPTION_CLEARED",
            "venue_schedule",
            "network",
            {"reason": reason, "closures_lifted": [closure_id], "transit_restored": []},
        )
        return self._replan_affected(reason=reason)

    def restore_transit(self, transit_id: str, reason: str = "transit_restored") -> dict[str, Any]:
        if transit_id not in self.suspended_transit:
            raise ServiceError("该班次未处于停运状态")
        self._emit(
            "DISRUPTION_CLEARED",
            "venue_schedule",
            "network",
            {"reason": reason, "closures_lifted": [], "transit_restored": [transit_id]},
        )
        return self._replan_affected(reason=reason)

    def _apply_session_change(self, session_id: str, start_at: str, latest_arrival_at: str) -> None:
        sessions = []
        for session in self.scenario.sessions:
            if session.session_id == session_id:
                sessions.append(
                    type(session)(
                        venue_id=session.venue_id,
                        session_id=session.session_id,
                        start_at=start_at,
                        latest_arrival_at=latest_arrival_at,
                        security_zone=session.security_zone,
                    )
                )
            else:
                sessions.append(session)
        self.scenario = type(self.scenario)(
            sessions=tuple(sessions),
            residences=self.scenario.residences,
            transit=self.scenario.transit,
            security_slots=self.scenario.security_slots,
            vehicles=self.scenario.vehicles,
            closures=self.scenario.closures,
        )

    def _replan_affected(self, reason: str) -> dict[str, Any]:
        affected: list[str] = []
        for state in list(self.journeys.values()):
            if state.status == STATUS_STRANDED:
                plan = self._plan(state.request)
                if plan.feasible:
                    affected.append(state.business_ref)
                    self._emit(
                        "JOURNEY_REPLANNED",
                        "journey_commitment",
                        state.business_ref,
                        {
                            "business_ref": state.business_ref,
                            "affected_segments": [],
                            "reason": f"recovered_after_{reason}",
                            "from_option": state.basis_option,
                            "was_dispatch": False,
                        },
                    )
                    self._propose(plan, reason=f"recovered_after_{reason}")
                continue
            if state.status not in ACTIVE_STATUSES:
                continue
            if state.status == STATUS_AWAITING_UPGRADE:
                basis = state.current.option if state.current else None
            else:
                basis = state.basis_option or (state.current.option if state.current else None)
            if basis is None:
                continue
            if not self._still_feasible(state.request, basis):
                affected.append(state.business_ref)
                self._replan_one(state, basis, reason)
        return {"action": "disruption", "reason": reason, "affected": affected}

    def _still_feasible(self, request: RequestInput, basis: Mapping[str, Any]) -> bool:
        plan = self._plan(request)
        if not plan.feasible:
            return False
        candidates = [plan.primary, *plan.backups]
        return any(
            option.resource_ref == basis["resource_ref"] and option.security_slot_id == basis["security_slot_id"]
            for option in candidates
        )

    def _replan_one(self, state: JourneyState, basis: Mapping[str, Any], reason: str) -> None:
        was_dispatch = state.status in DISPATCHED_STATUSES
        if was_dispatch:
            self._release(state, reason=reason)
        self._emit(
            "JOURNEY_REPLANNED",
            "journey_commitment",
            state.business_ref,
            {
                "business_ref": state.business_ref,
                "affected_segments": [
                    {"kind": "resource", "ref": basis["resource_ref"]},
                    {"kind": "security_slot", "ref": basis["security_slot_id"]},
                ],
                "reason": reason,
                "from_option": basis,
                "was_dispatch": was_dispatch,
            },
        )
        plan = self._plan(state.request, exclude_refs=frozenset({basis["resource_ref"]}))
        if not plan.feasible:
            plan = self._plan(state.request)
        if not plan.feasible:
            self._emit(
                "JOURNEY_STRANDED",
                "journey_commitment",
                state.business_ref,
                {"business_ref": state.business_ref, "reason": "stranded_after_disruption", "from_option": basis},
            )
            return
        new_option = plan.primary.to_dict()
        if not was_dispatch:
            # 尚未确认的行程只换方案，不自动占用资源。
            self._propose(plan, reason=reason)
            return
        old_cost = int(basis["cost"])
        if new_option["cost"] <= old_cost:
            self._propose(plan, reason=reason)
            self._reserve(state, new_option, reason=reason, event_type="DISPATCH_CONFIRMED", allow_preempt=True)
            return
        # 更贵的替代路径进入升级节点：宽限期内可人工确认，到期自动生效以保赛事义务。
        due = times.minutes_after(times.iso(self.clock.now), UPGRADE_GRACE_MINUTES)
        self._emit(
            "OPTION_PROPOSED",
            "journey_commitment",
            state.business_ref,
            {
                "business_ref": state.business_ref,
                "option": new_option,
                "reason": f"upgrade_after_{reason}",
                "previous_cost": old_cost,
                "upgrade_due_at": due,
            },
        )
        state.status = STATUS_AWAITING_UPGRADE
        state.upgrade_due_at = due
        state.basis_option = basis  # 保留旧依据直到升级落定

    def accept_upgrade(self, business_ref: str) -> dict[str, Any]:
        state = self._state(business_ref, {STATUS_AWAITING_UPGRADE})
        return self._accept_upgrade(state, state.current.option)

    def _accept_upgrade(self, state: JourneyState, option: Mapping[str, Any], automatic: bool = False) -> dict[str, Any]:
        self._reserve(
            state,
            option,
            reason="automatic_upgrade" if automatic else "upgrade_accepted",
            event_type="JOURNEY_UPGRADED",
            extra={"previous_cost": state.basis_option["cost"] if state.basis_option else None, "automatic": automatic},
            allow_preempt=True,
        )
        return {"action": "upgraded", "business_ref": state.business_ref, "option": option}

    # ------------------------------------------------------------------ 占用与抢占

    def _reserve(
        self,
        state: JourneyState,
        option: Mapping[str, Any],
        reason: str,
        event_type: str,
        extra: Mapping[str, Any] | None = None,
        allow_preempt: bool = True,
        _depth: int = 0,
        _claims: dict[str, int] | None = None,
    ) -> None:
        if _depth > MAX_PREEMPTION_DEPTH:
            raise ServiceError("抢占链路过长，避免连环改派")
        claims: dict[str, int] = dict(_claims or {})
        atoms = _atoms_for(option)
        contenders = self._contenders(state.request, atoms, claims)
        if contenders and not allow_preempt:
            raise ServiceError("资源争用：需要升级或改走备用路径")
        for holder in contenders:
            if not self._can_preempt(state.request, holder):
                raise ServiceError(f"资源被更高优先级或在途行程占用：{holder.business_ref}")
        # 预演：在模拟台账上为每个被挤掉者选定备选，全部成立才真正改派，保证原子性。
        alternatives = self._plan_displacements(state.request, atoms, contenders, claims)
        # 本层占用作为"已宣告"传给子改派，防止被挤掉者重新落到同一资源上。
        child_claims = dict(claims)
        for key, units in atoms:
            child_claims[key] = child_claims.get(key, 0) + units
        for holder in contenders:
            alternative = alternatives[holder.business_ref]
            basis = holder.basis_option
            self._release(holder, reason=f"preempted_by:{state.business_ref}")
            self._emit(
                "JOURNEY_REPLANNED",
                "journey_commitment",
                holder.business_ref,
                {
                    "business_ref": holder.business_ref,
                    "affected_segments": [{"kind": "resource", "ref": basis["resource_ref"]}],
                    "reason": f"preempted_by:{state.business_ref}",
                    "from_option": basis,
                },
            )
            alt_plan = self._plan(holder.request, exclude_refs=frozenset({basis["resource_ref"]}))
            self._propose_alt(holder, alt_plan, alternative, reason=f"preempted_by:{state.business_ref}")
            self._reserve(
                holder,
                alternative,
                reason=f"preempted_by:{state.business_ref}",
                event_type="DISPATCH_CONFIRMED",
                allow_preempt=True,
                _depth=_depth + 1,
                _claims=child_claims,
            )
        payload_atoms = [
            {"key": key, "units": units, "remaining": self._capacity_remaining(key, units, claims)}
            for key, units in atoms
        ]
        self._emit(
            "RESOURCE_RESERVED",
            "transport_resource",
            option["resource_ref"],
            {"business_ref": state.business_ref, "resource_ref": option["resource_ref"], "capacity": option["seats_required"], "atoms": payload_atoms, "reason": reason},
        )
        payload = {
            "business_ref": state.business_ref,
            "option": dict(option),
            "atoms": payload_atoms,
            "basis_nonce": state.nonce,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        self._emit(event_type, "journey_commitment", state.business_ref, payload)

    def _plan_displacements(
        self,
        challenger: RequestInput,
        atoms: list[tuple[str, int]],
        contenders: list[JourneyState],
        claims: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        """在模拟台账上安排：先撤下被挤掉者，放入争夺者，再逐户选不冲突的备选。"""
        sim = self._claimed_view(claims)
        for holder in contenders:
            for key, _units in _atoms_for(holder.basis_option):
                sim[key] -= self.holdings.get(key, {}).get(holder.business_ref, 0)
        for key, units in atoms:
            sim[key] = sim.get(key, 0) + units
        chosen: dict[str, dict[str, Any]] = {}
        for holder in contenders:
            plan = self._plan(holder.request, exclude_refs=frozenset({holder.basis_option["resource_ref"]}))
            if not plan.feasible:
                plan = self._plan(holder.request)
            if not plan.feasible:
                raise ServiceError(f"被抢占行程无备用路径：{holder.business_ref}")
            picked = None
            for candidate in (plan.primary, *plan.backups):
                data = candidate.to_dict()
                if all(sim.get(key, 0) + units <= self._atom_capacity(key) for key, units in _atoms_for(data)):
                    picked = data
                    break
            if picked is None:
                raise ServiceError(f"被抢占行程的备用路径仍冲突：{holder.business_ref}")
            chosen[holder.business_ref] = picked
            for key, units in _atoms_for(picked):
                sim[key] = sim.get(key, 0) + units
        return chosen

    def _claimed_view(self, claims: dict[str, int]) -> dict[str, int]:
        view: dict[str, int] = {}
        for key, holders in self.holdings.items():
            view[key] = view.get(key, 0) + sum(holders.values())
        for key, units in claims.items():
            view[key] = view.get(key, 0) + units
        return view

    def _propose_alt(self, holder: JourneyState, plan: Plan, alternative: Mapping[str, Any], reason: str) -> None:
        backups = [o.to_dict() for o in (plan.primary, *plan.backups) if o.option_id != alternative["option_id"]]
        self._emit(
            "OPTION_PROPOSED",
            "journey_commitment",
            holder.business_ref,
            {"business_ref": holder.business_ref, "option": dict(alternative), "reason": reason, "backups": backups},
        )

    def _contenders(
        self, request: RequestInput, atoms: list[tuple[str, int]], claims: dict[str, int] | None = None
    ) -> list[JourneyState]:
        """只选出释放容量所必需的、最弱的一批占用人（跨原子并集）。"""
        claims = claims or {}
        chosen: dict[str, JourneyState] = {}
        for key, units in atoms:
            capacity = self._atom_capacity(key)
            used = sum(self.holdings.get(key, {}).values()) + claims.get(key, 0)
            owner_units = self.holdings.get(key, {}).get(request.business_ref, 0)
            deficit = units - owner_units - (capacity - used)
            if deficit <= 0:
                continue
            ranked = sorted(
                ((self._preempt_order(self.journeys[ref]), held, ref)
                 for ref, held in self.holdings.get(key, {}).items()
                 if ref != request.business_ref),
                key=lambda item: item[0],
            )
            freed = 0
            for _order, held, ref in ranked:
                if freed >= deficit:
                    break
                chosen[ref] = self.journeys[ref]
                freed += held
        return list(chosen.values())

    def _preempt_order(self, holder: JourneyState) -> tuple[int, int]:
        """排序键：越弱越先被挤；已发车/受保护档永远排在最后。"""
        basis = holder.basis_option
        departed = holder.status in DISPATCHED_STATUSES and basis is not None and times.parse(basis["departs_at"]) <= self.clock.now
        tier = holder.request.need_tier
        if departed or tier in (NEED_MEDICAL, NEED_ACCESSIBLE):
            return (9, 9)
        return (0, -ROLE_RANK[holder.request.role])

    def _can_preempt(self, challenger: RequestInput, holder: JourneyState) -> bool:
        if holder.status not in ACTIVE_STATUSES:
            return False
        basis = holder.basis_option
        if basis is None:
            return False
        # 已到出发时刻：车辆/班次已在服务，不能抽走；完成的转运更不可动。
        if holder.status in DISPATCHED_STATUSES and times.parse(basis["departs_at"]) <= self.clock.now:
            return False
        challenger_tier = challenger.need_tier
        holder_tier = holder.request.need_tier
        protected = {NEED_MEDICAL, NEED_ACCESSIBLE}
        if holder_tier in protected:
            return False  # 医疗与无障碍不能被普通优先级，也不能被彼此覆盖
        if challenger_tier in protected:
            return True
        return ROLE_RANK[challenger.role] > ROLE_RANK[holder.request.role]

    def _release(self, state: JourneyState, reason: str) -> None:
        if not state.held_atoms:
            return
        option = state.basis_option or {}
        atoms = [{"key": key, "units": units} for key, units in _atoms_for(option) if key in state.held_atoms]
        self._emit(
            "RESOURCE_RELEASED",
            "transport_resource",
            option.get("resource_ref", state.business_ref),
            {
                "business_ref": state.business_ref,
                "resource_ref": option.get("resource_ref"),
                "atoms": atoms,
                "reason": reason,
            },
        )
        state.held_atoms = []

    # ------------------------------------------------------------------ 查询/审计

    def explain(self, ref_or_person: str) -> dict[str, Any]:
        state = self.journeys.get(ref_or_person)
        if state is None:
            state = next((s for s in self.journeys.values() if s.request.person_id == ref_or_person), None)
        if state is None:
            raise ServiceError("未找到该业务编号或人员")
        option = state.basis_option or (state.current.option if state.current else None)
        return {
            "business_ref": state.business_ref,
            "person": {"person_id": state.request.person_id, "name": state.request.person_name, "role": state.request.role},
            "status": state.status,
            "need_tier": state.request.need_tier,
            "current_option": option,
            "why": self._why(state, option),
            "held_atoms": state.held_atoms,
            "expires_at": state.expires_at,
            "upgrade_due_at": state.upgrade_due_at,
            "timeline": [
                {"event_id": e["event_id"], "at": e["occurred_at"], "type": e["event_type"], "payload": e["payload"]}
                for e in self.events
                if (
                    e["aggregate_id"] == state.business_ref
                    and e["aggregate_type"] in ("travel_request", "journey_commitment")
                )
                or (
                    e["aggregate_type"] == "transport_resource"
                    and e["payload"].get("business_ref") == state.business_ref
                )
            ],
        }

    def _why(self, state: JourneyState, option: Mapping[str, Any] | None) -> list[str]:
        if option is None:
            return ["当前无可行方案"]
        why = list(option.get("why", ()))
        req = state.request
        if req.medical:
            why.append("医疗需求为最高保护档，普通优先级不可覆盖")
        elif req.accessible:
            why.append("无障碍需求受保护，普通优先级不可覆盖")
        replanned = [e for e in self.events if e["event_type"] == "JOURNEY_REPLANNED" and e["payload"]["business_ref"] == state.business_ref]
        if replanned and state.basis_option is not None:
            why.append(f"当前方案为变更后的替代路径（经历 {len(replanned)} 次重排），原方案与资源释放见时间线")
        return why

    def resource_ledger(self) -> dict[str, Any]:
        ledger = {}
        for key, holders in sorted(self.holdings.items()):
            if not holders:
                continue
            capacity = self._atom_capacity(key)
            ledger[key] = {
                "capacity": capacity,
                "used": sum(holders.values()),
                "held_by": dict(holders),
                "remaining": capacity - sum(holders.values()),
            }
        movements = [
            {
                "event_id": e["event_id"],
                "at": e["occurred_at"],
                "type": e["event_type"],
                "business_ref": e["payload"].get("business_ref"),
                "resource_ref": e["payload"].get("resource_ref"),
                "atoms": e["payload"].get("atoms"),
                "reason": e["payload"].get("reason"),
            }
            for e in self.events
            if e["event_type"] in ("RESOURCE_RESERVED", "RESOURCE_RELEASED")
        ]
        return {"holdings": ledger, "movements": movements}

    def changes(self) -> list[dict[str, Any]]:
        return [
            {
                "event_id": e["event_id"],
                "business_ref": e["payload"]["business_ref"],
                "at": e["occurred_at"],
                "reason": e["payload"]["reason"],
                "affected_segments": e["payload"].get("affected_segments"),
                "from_option": e["payload"].get("from_option"),
            }
            for e in self.events
            if e["event_type"] == "JOURNEY_REPLANNED"
        ]

    def pending_obligations(self) -> list[dict[str, Any]]:
        result = []
        for state in self.journeys.values():
            if state.status in (STATUS_COMPLETED, STATUS_CLOSED, STATUS_REJECTED):
                continue
            result.append(
                {
                    "business_ref": state.business_ref,
                    "status": state.status,
                    "person_id": state.request.person_id,
                    "expires_at": state.expires_at,
                    "upgrade_due_at": state.upgrade_due_at,
                }
            )
        return result

    # ------------------------------------------------------------------ 内部工具

    def _state(self, business_ref: str, allowed: set[str] | None = None) -> JourneyState:
        state = self.journeys.get(business_ref)
        if state is None:
            raise ServiceError("未知业务编号")
        if allowed is not None and state.status not in allowed:
            raise ServiceError(f"当前状态 {state.status} 不允许该操作")
        return state

    def _required_arrival(self, request: RequestInput) -> str:
        session = self.scenario.session(request.session_id)
        return session.latest_arrival_at if session else ""

    def _effective_scenario(self) -> Scenario:
        from .model import RouteClosure

        transit = tuple(
            type(t)(
                transit_id=t.transit_id,
                residence_id=t.residence_id,
                venue_id=t.venue_id,
                departs_at=t.departs_at,
                arrives_at=t.arrives_at,
                seats=t.seats,
                suspended=t.suspended or t.transit_id in self.suspended_transit,
            )
            for t in self.scenario.transit
        )
        closure_objects = list(self.scenario.closures)
        for item in self.closures:
            closure_objects.append(
                RouteClosure(
                    closure_id=item["closure_id"],
                    residence_id=item["residence_id"],
                    venue_id=item["venue_id"],
                    starts_at=item["starts_at"],
                    ends_at=item["ends_at"],
                )
            )
        return type(self.scenario)(
            sessions=self.scenario.sessions,
            residences=self.scenario.residences,
            transit=transit,
            security_slots=self.scenario.security_slots,
            vehicles=self.scenario.vehicles,
            closures=tuple(closure_objects),
        )

    def _plan(self, request: RequestInput, exclude_refs: frozenset[str] = frozenset()) -> Plan:
        return plan_request(request, self._effective_scenario(), exclude_refs=exclude_refs)

    def _propose(self, plan: Plan, reason: str) -> None:
        self._emit(
            "OPTION_PROPOSED",
            "journey_commitment",
            plan.business_ref,
            {"business_ref": plan.business_ref, "option": plan.primary.to_dict(), "reason": reason, "backups": [o.to_dict() for o in plan.backups]},
        )

    def _select_option(self, request: RequestInput, option_id: str | None) -> dict[str, Any]:
        plan = self._plan(request)
        if not plan.feasible:
            raise ServiceError("当前无可行方案")
        if option_id is None:
            return plan.primary.to_dict()
        candidates = [plan.primary, *plan.backups]
        for option in candidates:
            if option.option_id == option_id:
                return option.to_dict()
        raise ServiceError("备选方案不存在或已不再可行")

    def _choose_option(self, request: RequestInput) -> dict[str, Any]:
        """确认时的实时选择：主方案若被占且不可抢占，顺次尝试更贵的候选。"""
        plan = self._plan(request)
        if not plan.feasible:
            raise ServiceError("当前无可行方案")
        candidates = [plan.primary, *plan.backups]
        for option in candidates:
            data = option.to_dict()
            contenders = self._contenders(request, _atoms_for(data))
            if all(self._can_preempt(request, holder) for holder in contenders):
                return data
        raise ServiceError("所有候选路径均被不可覆盖的行程占用")

    def _atom_capacity(self, key: str) -> int:
        kind, ref = key.split(":", 1)
        if kind == "vehicle":
            vehicle = next(v for v in self.scenario.vehicles if v.vehicle_id == ref)
            return 1
        if kind == "transit":
            t = next(t for t in self.scenario.transit if t.transit_id == ref)
            return t.seats
        slot = next(s for s in self.scenario.security_slots if s.slot_id == ref)
        return slot.capacity

    def _capacity_remaining(self, key: str, incoming: int, claims: dict[str, int] | None = None) -> int:
        return self._atom_capacity(key) - sum(self.holdings.get(key, {}).values()) - (claims or {}).get(key, 0) - incoming


def _request_from_payload(data: Mapping[str, Any]) -> RequestInput:
    return RequestInput(
        business_ref=data["business_ref"],
        person_id=data["person_id"],
        person_name=data.get("person_name", data["person_id"]),
        role=data["role"],
        residence_id=data["residence_id"],
        venue_id=data["venue_id"],
        session_id=data["session_id"],
        party_size=int(data.get("party_size", 1)),
        accessible=bool(data.get("accessible", False)),
        medical=bool(data.get("medical", False)),
        equipment_units=int(data.get("equipment_units", 0)),
        notification_key=data.get("notification_key"),
    )


def _fingerprint(request: RequestInput) -> tuple[str, str, str, str, int, bool, bool, int, str]:
    return (
        request.person_id,
        request.role,
        request.session_id,
        request.residence_id,
        request.party_size,
        request.accessible,
        request.medical,
        request.equipment_units,
        request.venue_id,
    )


def _keys(request: RequestInput) -> list[str]:
    return [request.notification_key] if request.notification_key else []
