"""分散赛区通勤保障服务。

在事件契约之上实现：

* 申报受理：重复通知幂等不重复派车；业务编号相同而人员/时间/路线不同进入核查；
* 方案形成：规划器给出可解释方案，车辆、座位与安检时段在同一事件批次内一次性占用；
* 时钟节点：确认 → 候车 → 登乘 → 完成，超时失约，无可行方案则升级；
* 增量重排：停运、管制、车辆下线、窗口关闭或临时改项只重排真正受影响的开放行程，
  已完成转运保留当时依据不被回溯；
* 恢复：重放事件日志后继续未完成的保障义务（候车计时、失约判定、升级重试）。

命令对象只负责“算出事件并整批提交”，内存状态一律由 ``_apply`` 投影得到，
因此重放日志与在线运行到达完全一致的状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from .events import Clock, Event, EventStore
from .model import Registry, TravelRequest, TravelerRole
from .planner import (
    Hold,
    Option,
    Planner,
    PlanResult,
    Reservation,
    ResourceInventory,
    planning_order_key,
    shortest_path,
)

WAIT_LEAD = timedelta(minutes=15)        # 发车前 15 分钟进入候车
NO_SHOW_GRACE = timedelta(minutes=10)    # 发车后 10 分钟未登乘按失约处理


def _dt(value: str | datetime) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


@dataclass
class PlannedReservation:
    resource_ref: str
    kind: str
    units: int
    capacity: int
    start: datetime
    end: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "resource_ref": self.resource_ref,
            "kind": self.kind,
            "units": self.units,
            "capacity": self.capacity,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "PlannedReservation":
        return cls(
            resource_ref=raw["resource_ref"], kind=raw["kind"],
            units=raw["units"], capacity=raw["capacity"],
            start=_dt(raw["start"]), end=_dt(raw["end"]),
        )

    @classmethod
    def from_option(cls, r: Reservation) -> "PlannedReservation":
        return cls(r.resource_ref, r.kind, r.units, r.capacity, r.start, r.end)


@dataclass
class JourneyRecord:
    journey_id: str
    request_id: str
    node: str = "CONFIRMED"
    option_id: str = ""
    mode: str = ""
    label: str = ""
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    cost: int = 0
    reservations: list[PlannedReservation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    basis_event_ids: list[str] = field(default_factory=list)
    closed_outcome: str | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    waiting_due: datetime | None = None
    no_show_deadline: datetime | None = None
    complete_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.closed_outcome is None

    def resource_refs(self) -> list[str]:
        return [r.resource_ref for r in self.reservations]


@dataclass
class Quarantine:
    request_id: str
    business_ref: str
    conflict_fields: list[str]
    existing_request_id: str
    detail: str
    incoming: TravelRequest


class IllegalTransition(Exception):
    def __init__(self, journey_id: str, node: str, target: str) -> None:
        super().__init__(f"行程 {journey_id} 当前节点 {node}，不能转入 {target}")
        self.journey_id = journey_id
        self.node = node
        self.target = target


class MobilityService:
    def __init__(self, registry: Registry, store: EventStore, clock: Clock,
                 wait_lead: timedelta = WAIT_LEAD,
                 no_show_grace: timedelta = NO_SHOW_GRACE) -> None:
        self.registry = registry
        self.store = store
        self.clock = clock
        self.wait_lead = wait_lead
        self.no_show_grace = no_show_grace
        self.inventory = ResourceInventory()
        self.planner = Planner(registry, self.inventory)
        self.requests: dict[str, TravelRequest] = {}
        self.journeys: dict[str, JourneyRecord] = {}
        self._by_notice: dict[str, str] = {}
        self._business: dict[str, dict[tuple, str]] = {}
        self._quarantine: dict[str, Quarantine] = {}
        self._pending: dict[str, int] = {}
        self._seq = 0
        if len(store):
            self._replay()

    @classmethod
    def restore(cls, registry: Registry, store: EventStore,
                clock: Clock) -> "MobilityService":
        """重放日志并立即续办：补走到期节点、重试升级义务。"""
        service = cls(registry, store, clock)
        service.tick()
        return service

    # ================================================================
    # 事件构造与提交
    # ================================================================

    def _next_id(self) -> str:
        self._seq += 1
        return f"evt-{self._seq:06d}"

    def _event(self, event_type: str, aggregate_type: str, aggregate_id: str,
               payload: dict[str, Any]) -> Event:
        # 版本号必须计入同一批次中已构造但尚未落盘的事件：多个行程可能在
        # 同一批次内先后释放/重占同一个资源聚合（如同一安检窗口）。
        n = self._pending.get(aggregate_id, 0) + 1
        self._pending[aggregate_id] = n
        return Event(
            event_id=self._next_id(),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=self.clock.now(),
            version=self.store.version_of(aggregate_id) + n,
            payload=payload,
        )

    def _commit(self, events: list[Event]) -> list[Event]:
        """整批落盘成功后再逐条投影；任一事件不合法则整批拒绝。"""
        if not events:
            return []
        try:
            committed = self.store.append_many(events)
        finally:
            for event in events:
                self._pending[event.aggregate_id] -= 1
                if self._pending[event.aggregate_id] == 0:
                    del self._pending[event.aggregate_id]
        for event in committed:
            self._apply(event)
        return committed

    # ================================================================
    # 申报受理（幂等 / 核查 / 争用裁决）
    # ================================================================

    def submit(self, request: TravelRequest) -> list[Event]:
        return self.submit_many([request])

    def submit_many(self, requests: list[TravelRequest]) -> list[Event]:
        """批量受理：重复通知幂等；同业务编号异内容核查；其余按硬门槛与
        角色优先级裁决后一次性占用资源。"""
        events: list[Event] = []
        fresh: list[TravelRequest] = []
        for request in requests:
            if request.notice_id in self._by_notice:
                # 同一份通知重复送达：幂等忽略，绝不重复派车
                continue
            same_business = self._business.get(request.business_ref, {})
            if request.signature() in same_business:
                # 业务编号与内容指纹都一致：换了通知编号的重复申报
                self._by_notice[request.notice_id] = same_business[request.signature()]
                continue
            if same_business:
                existing_id = next(iter(same_business.values()))
                conflict = self._conflict_fields(request, self.requests[existing_id])
                payload = self._request_payload(request)
                payload.update({
                    "business_ref": request.business_ref,
                    "conflict_fields": conflict,
                    "existing_request_id": existing_id,
                    "detail": (f"业务编号 {request.business_ref} 已存在申报 {existing_id}，"
                               f"但 {'、'.join(conflict)} 不一致，进入核查，暂不派车"),
                })
                events.append(self._event(
                    "REQUEST_QUARANTINED", "travel_request",
                    request.request_id, payload))
                continue
            fresh.append(request)

        for request in fresh:
            events.append(self._event(
                "REQUEST_ACCEPTED", "travel_request", request.request_id,
                self._request_payload(request)))

        # 争用裁决在试算台账上按优先级顺序进行，事件最后一次性提交
        trial = self.inventory.snapshot()
        trial_planner = Planner(self.registry, trial)
        plans: list[PlanResult] = []
        for request in sorted(fresh, key=planning_order_key):
            result = trial_planner.plan(request, now=self.clock.now())
            plans.append(result)
            if result.chosen is not None:
                assert trial.acquire([
                    Reservation(r.resource_ref, r.kind, r.units, r.capacity,
                                r.start, r.end).as_hold(request.request_id)
                    for r in result.chosen.reservations
                ])

        for request, result in zip(sorted(fresh, key=planning_order_key), plans):
            events.extend(self._plan_events(request, result))

        self._commit(events)
        return events

    @staticmethod
    def _conflict_fields(a: TravelRequest, b: TravelRequest) -> list[str]:
        found: list[str] = []
        if sorted(a.person_ids) != sorted(b.person_ids):
            found.append("人员")
        if a.required_arrival != b.required_arrival:
            found.append("时间")
        if (a.origin_lodging_id, a.duty_id) != (b.origin_lodging_id, b.duty_id):
            found.append("路线")
        return found

    def _request_payload(self, request: TravelRequest) -> dict[str, Any]:
        return {
            "business_ref": request.business_ref,
            "notice_id": request.notice_id,
            "person_ids": list(request.person_ids),
            "traveler_role": request.role.value,
            "required_arrival": request.required_arrival.isoformat(),
            "party_size": request.party_size,
            "wheelchair_count": request.wheelchair_count,
            "medical": request.medical,
            "equipment_units": request.equipment_units,
            "origin_lodging_id": request.origin_lodging_id,
            "duty_id": request.duty_id,
            "declared_at": request.declared_at.isoformat(),
            "note": request.note,
        }

    def resolve_quarantine(self, quarantined_request_id: str,
                           *, use_incoming: bool) -> list[Event]:
        """人工核查结论。

        ``use_incoming=False``：以既有申报为准，新通知作废；
        ``use_incoming=True``：以新申报为准，旧行程取消并就新申报派车。
        """
        q = self._quarantine.pop(quarantined_request_id)
        if not use_incoming:
            # 以既有申报为准：新通知作废，不产生新的保障事件
            return []
        old = self.requests[q.existing_request_id]
        request = q.incoming
        events: list[Event] = []
        for journey in self.journeys_for_request(old.request_id):
            if journey.open:
                events.extend(self._close_events(
                    journey, "cancelled",
                    f"核查后以新申报（{q.request_id}）为准，旧行程撤销"))
        events.append(self._event(
            "REQUEST_ACCEPTED", "travel_request", q.request_id,
            self._request_payload(request)))
        # 先提交撤销（投影释放旧占用），再按新申报规划并占用
        self._commit(events)
        result = self.planner.plan(request, now=self.clock.now())
        return self._commit(self._plan_events(request, result))

    # ================================================================
    # 方案事件
    # ================================================================

    def deadline_for(self, request: TravelRequest) -> datetime:
        duty = self.registry.duties[request.duty_id]
        return min(request.required_arrival, duty.ready_at) - self.planner.security_buffer

    def _plan_events(self, request: TravelRequest, result: PlanResult) -> list[Event]:
        journey_id = f"J-{request.request_id}"
        if result.chosen is None:
            return [
                self._event("JOURNEY_PLANNED", "journey_commitment", journey_id, {
                    "request_id": request.request_id,
                    "option_id": "", "mode": "", "label": "",
                    "depart_at": None, "arrive_at": None, "cost": 0,
                    "reasons": [],
                    "escalation_reasons": result.escalation_reasons,
                }),
                self._event("JOURNEY_LIFECYCLED", "journey_commitment", journey_id, {
                    "node": "ESCALATED",
                    "at_time": self.clock.now().isoformat(),
                    "reasons": result.escalation_reasons,
                }),
            ]
        option = result.chosen
        return [
            self._event("JOURNEY_PLANNED", "journey_commitment", journey_id,
                        self._option_payload(option, request.request_id, {
                            "rejected_options": self._rejected_summary(result.alternatives),
                        })),
            *self._reserve_events(journey_id, request.request_id, option, option.reservations),
        ]

    @staticmethod
    def _rejected_summary(alternatives: list[Option]) -> list[dict[str, Any]]:
        return [{"option_id": o.option_id, "label": o.label, "mode": o.mode,
                 "rejections": o.rejections} for o in alternatives[:5]]

    def _reserve_events(self, journey_id: str, request_id: str, option: Option,
                        reservations: Iterable[Reservation]) -> list[Event]:
        return [
            self._event("RESOURCE_RESERVED", "transport_resource",
                        r.resource_ref, {
                "resource_ref": r.resource_ref,
                "capacity": r.capacity,
                "units": r.units,
                "kind": r.kind,
                "start": r.start.isoformat(),
                "end": r.end.isoformat(),
                "journey_id": journey_id,
                "request_id": request_id,
                "mode": option.mode,
            })
            for r in reservations
        ]

    def _release_events(self, record: JourneyRecord, refs: Iterable[str],
                        reason: str, *,
                        trigger_event_id: str | None = None) -> list[Event]:
        by_ref = {r.resource_ref: r for r in record.reservations}
        events = []
        for ref in refs:
            reservation = by_ref[ref]
            events.append(self._event(
                "RESOURCE_RELEASED", "transport_resource", ref, {
                    "resource_ref": ref,
                    "capacity": reservation.capacity,
                    "units": reservation.units,
                    "kind": reservation.kind,
                    "start": reservation.start.isoformat(),
                    "end": reservation.end.isoformat(),
                    "journey_id": record.journey_id,
                    "request_id": record.request_id,
                    "reason": reason,
                    "trigger_event_id": trigger_event_id,
                }))
        return events

    def _option_payload(self, option: Option, request_id: str,
                        extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "request_id": request_id,
            "option_id": option.option_id,
            "mode": option.mode,
            "label": option.label,
            "depart_at": option.depart_at.isoformat(),
            "arrive_at": option.arrive_at.isoformat(),
            "cost": option.cost,
            "reasons": option.reasons,
            "reservations": [
                PlannedReservation.from_option(r).to_payload()
                for r in option.reservations
            ],
        }
        if extra:
            payload.update(extra)
        return payload

    def _close_events(self, record: JourneyRecord, outcome: str,
                      reason: str) -> list[Event]:
        events: list[Event] = []
        if record.reservations:
            # 失约是提前释放；完成是资源到期释放——两种闭环都让容量回归台账
            release_reason = (reason if outcome == "cancelled"
                              else "转运完成，车辆/座位/安检时段到期释放")
            events.extend(self._release_events(
                record, record.resource_refs(), release_reason))
        node = "COMPLETED" if outcome == "completed" else "NO_SHOW"
        events.append(self._event(
            "JOURNEY_LIFECYCLED", "journey_commitment", record.journey_id,
            {"node": node, "at_time": self.clock.now().isoformat(), "reason": reason}))
        events.append(self._event(
            "COMMITMENT_CLOSED", "journey_commitment", record.journey_id, {
                "outcome": outcome,
                "basis_event_ids": list(record.basis_event_ids),
                "closed_at": self.clock.now().isoformat(),
                "reason": reason,
            }))
        return events

    # ================================================================
    # 时钟节点
    # ================================================================

    def acknowledge(self, journey_id: str) -> list[Event]:
        """代表团确认收到出发方案。"""
        record = self.journeys[journey_id]
        return self._commit([self._event(
            "JOURNEY_LIFECYCLED", "journey_commitment", journey_id,
            {"node": "CONFIRMED", "at_time": self.clock.now().isoformat(),
             "acknowledged": True})])

    def board(self, journey_id: str) -> list[Event]:
        """现场登乘；只在候车节点有效，到达闭环仍由时钟完成。"""
        record = self.journeys[journey_id]
        if record.node != "WAITING":
            raise IllegalTransition(journey_id, record.node, "BOARDED")
        return self._commit([self._event(
            "JOURNEY_LIFECYCLED", "journey_commitment", journey_id,
            {"node": "BOARDED", "at_time": self.clock.now().isoformat()})])

    def tick(self) -> list[Event]:
        """把所有行程推进到当前时钟应处的稳定节点；可安全重复调用（幂等）。

        例如恢复时若当前时刻已超过失约宽限，同一行程会在一次 tick 内连续
        走过候车、失约两个节点，把崩溃前未完成的保障义务补齐。
        登乘是现场人工动作，不会被时钟自动跳过。
        """
        produced: list[Event] = []
        now = self.clock.now()
        for record in list(self.journeys.values()):
            moved = True
            while moved and record.open:
                moved = False
                if (record.node == "CONFIRMED" and record.waiting_due
                        and now >= record.waiting_due):
                    produced.extend(self._commit([self._event(
                        "JOURNEY_LIFECYCLED", "journey_commitment", record.journey_id, {
                            "node": "WAITING", "at_time": now.isoformat(),
                            "depart_at": record.depart_at.isoformat()
                            if record.depart_at else None,
                        })]))
                    moved = True
                elif (record.node == "WAITING" and record.no_show_deadline
                        and now >= record.no_show_deadline):
                    produced.extend(self._commit(self._close_events(
                        record, "cancelled",
                        f"超过发车宽限 {int(self.no_show_grace.total_seconds() // 60)} "
                        "分钟未登乘，按失约处理")))
                    moved = True
                elif (record.node == "BOARDED" and record.complete_at
                        and now >= record.complete_at):
                    produced.extend(self._commit(
                        self._close_events(record, "completed", "按时到达闭环")))
                    moved = True
        # 升级中的义务每次推进都尝试获得新方案
        for record in list(self.journeys.values()):
            if record.open and record.node == "ESCALATED":
                new_events = self._retry_escalation(record)
                if new_events:
                    produced.extend(new_events)
        return produced

    # ================================================================
    # 供给变化与增量重排
    # ================================================================

    def suspend_trip(self, trip_id: str) -> list[Event]:
        return self._supply_change(
            "transit_suspension", [f"trip:{trip_id}"],
            lambda: self.registry.set_trip_suspended(trip_id, True),
            f"公共交通班次 {trip_id} 停运")

    def restore_trip(self, trip_id: str) -> list[Event]:
        return self._supply_change(
            "transit_suspension", [f"trip:{trip_id}"],
            lambda: self.registry.set_trip_suspended(trip_id, False),
            f"公共交通班次 {trip_id} 恢复运行", restoring=True)

    def restrict_road(self, segment_id: str) -> list[Event]:
        return self._supply_change(
            "road_restriction", [f"segment:{segment_id}"],
            lambda: self.registry.set_segment_restricted(segment_id, True),
            f"路段 {segment_id} 临时管制")

    def lift_road_restriction(self, segment_id: str) -> list[Event]:
        return self._supply_change(
            "road_restriction", [f"segment:{segment_id}"],
            lambda: self.registry.set_segment_restricted(segment_id, False),
            f"路段 {segment_id} 解除管制", restoring=True)

    def take_vehicle_offline(self, vehicle_id: str) -> list[Event]:
        return self._supply_change(
            "vehicle_offline", [f"vehicle:{vehicle_id}"],
            lambda: self.registry.set_vehicle_offline(vehicle_id, True),
            f"车辆 {vehicle_id} 临时下线")

    def return_vehicle_online(self, vehicle_id: str) -> list[Event]:
        return self._supply_change(
            "vehicle_offline", [f"vehicle:{vehicle_id}"],
            lambda: self.registry.set_vehicle_offline(vehicle_id, False),
            f"车辆 {vehicle_id} 恢复运营", restoring=True)

    def close_security_window(self, window_id: str) -> list[Event]:
        return self._supply_change(
            "security_window_closed", [f"window:{window_id}"],
            lambda: self.registry.set_window_closed(window_id, True),
            f"安检窗口 {window_id} 关闭")

    def reopen_security_window(self, window_id: str) -> list[Event]:
        return self._supply_change(
            "security_window_closed", [f"window:{window_id}"],
            lambda: self.registry.set_window_closed(window_id, False),
            f"安检窗口 {window_id} 重新开放", restoring=True)

    def change_schedule(self, duty_id: str, ready_at: datetime,
                        window_ids: tuple[str, ...] | None = None) -> list[Event]:
        old = self.registry.duties[duty_id]
        event = self._event("SCHEDULE_CHANGED", "venue_schedule", f"duty:{duty_id}", {
            "change_kind": "schedule_change",
            "affected_refs": [f"duty:{duty_id}"],
            "effective_from": self.clock.now().isoformat(),
            "duty_id": duty_id,
            "old_ready_at": old.ready_at.isoformat(),
            "new_ready_at": ready_at.isoformat(),
            "window_ids": list(window_ids) if window_ids is not None else list(old.window_ids),
            "reason": f"赛时义务 {duty_id}（{old.label}）临时改项",
        })
        self._commit([event])
        self.registry.reschedule_duty(duty_id, ready_at, window_ids)
        produced = [event]
        produced.extend(self._replan_affected(
            {"duty"}, f"赛时义务 {duty_id} 临时改项", trigger_event_id=event.event_id))
        # 改期（如就绪时刻推迟）可能让此前升级的义务重新可行
        for record in list(self.journeys.values()):
            if record.open and record.node == "ESCALATED":
                new_events = self._retry_escalation(record)
                if new_events:
                    produced.extend(new_events)
        return produced

    def _supply_change(self, kind: str, refs: list[str],
                       apply_change: Callable[[], None], reason: str,
                       *, restoring: bool = False) -> list[Event]:
        event = self._event("SUPPLY_CHANGED", "transport_resource",
                            "supply:" + refs[0], {
            "change_kind": kind,
            "affected_refs": refs,
            "effective_from": self.clock.now().isoformat(),
            "reason": reason,
            "restored": restoring,
        })
        self._commit([event])
        apply_change()
        affected_kinds = {r.split(":", 1)[0] for r in refs}
        produced = [event]
        produced.extend(self._replan_affected(
            affected_kinds, reason, trigger_event_id=event.event_id))
        if restoring:
            for record in list(self.journeys.values()):
                if record.open and record.node == "ESCALATED":
                    new_events = self._retry_escalation(record)
                    if new_events:
                        produced.extend(new_events)
        return produced

    # -- 增量重排 --------------------------------------------------------

    def _replan_affected(self, affected_kinds: set[str], reason: str,
                         *, trigger_event_id: str | None = None) -> list[Event]:
        """只重排当前方案被变化破坏的开放行程；已完成行程不回溯。"""
        affected = [r for r in self.journeys.values()
                    if r.open and self._is_affected(r, affected_kinds)]
        if not affected:
            return []
        affected.sort(key=lambda r: planning_order_key(self.requests[r.request_id]))

        # 试算台账：摘掉受影响行程的旧占用，其余行程（含已完成的历史转运）保留
        affected_request_ids = {r.request_id for r in affected}
        trial = ResourceInventory.from_holds([
            hold for hold in self.inventory.holds()
            if hold.request_id not in affected_request_ids
        ])
        trial_planner = Planner(self.registry, trial)
        decisions: list[tuple[JourneyRecord, Option | None, list[str]]] = []
        for record in affected:
            request = self.requests[record.request_id]
            result = trial_planner.plan(request, now=self.clock.now())
            if result.chosen is not None:
                acquired = trial.acquire([
                    r.as_hold(request.request_id) for r in result.chosen.reservations
                ])
                assert acquired
            decisions.append((record, result.chosen, result.escalation_reasons))

        # 所有释放/重占/升级在一个事件批次内提交
        events: list[Event] = []
        for record, option, escalation in decisions:
            if option is None:
                reasons = escalation + [f"触发原因：{reason}"]
                events.extend(self._release_events(
                    record, record.resource_refs(), "现有方案失效且暂无替代，升级处理",
                    trigger_event_id=trigger_event_id))
                events.append(self._event(
                    "JOURNEY_LIFECYCLED", "journey_commitment", record.journey_id,
                    {"node": "ESCALATED", "at_time": self.clock.now().isoformat(),
                     "reasons": reasons, "reason": reason,
                     "trigger_event_id": trigger_event_id}))
                continue
            old_refs = record.resource_refs()
            events.extend(self._release_events(
                record, old_refs, f"重排释放：{reason}",
                trigger_event_id=trigger_event_id))
            events.append(self._event(
                "JOURNEY_REPLANNED", "journey_commitment", record.journey_id,
                self._option_payload(option, record.request_id, {
                    "affected_segments": sorted(
                        set(old_refs) | {r.resource_ref for r in option.reservations}),
                    "reason": reason,
                    "old_option_id": record.option_id,
                    "rejected_options": [],
                    "trigger_event_id": trigger_event_id,
                })))
            events.extend(self._reserve_events(
                record.journey_id, record.request_id, option, option.reservations))
            events.append(self._event(
                "JOURNEY_LIFECYCLED", "journey_commitment", record.journey_id,
                {"node": "CONFIRMED", "at_time": self.clock.now().isoformat(),
                 "replanned": True, "reason": reason,
                 "trigger_event_id": trigger_event_id}))
        return self._commit(events)

    def _is_affected(self, record: JourneyRecord, kinds: set[str]) -> bool:
        refs = record.resource_refs()
        request = self.requests[record.request_id]
        if "trip" in kinds:
            for ref in refs:
                if ref.startswith("transit:"):
                    trip_id = ref.split(":")[1]
                    if self.registry.trips[trip_id].suspended:
                        return True
        if "vehicle" in kinds:
            for ref in refs:
                if ref.startswith("vehicle:"):
                    vehicle_id = ref.split(":")[1]
                    if self.registry.vehicles[vehicle_id].offline:
                        return True
        if "window" in kinds:
            for ref in refs:
                if ref.startswith("window:"):
                    if self.registry.windows[ref.split(":", 1)[1]].closed:
                        return True
        if "segment" in kinds and record.mode in ("dedicated", "shuttle"):
            origin = self.registry.node_of_lodging(request.origin_lodging_id)
            venue_node = self.registry.venue_of_duty(request.duty_id).node
            leg = shortest_path(self.registry, origin, venue_node)
            if leg is None:
                return True
            if record.depart_at is not None and record.depart_at + timedelta(minutes=leg) > self.deadline_for(request):
                return True
        if "duty" in kinds and record.arrive_at is not None:
            if record.arrive_at > self.deadline_for(request):
                return True
        return False

    def _retry_escalation(self, record: JourneyRecord) -> list[Event] | None:
        """升级义务在供给恢复或容量释放后重新试算。"""
        request = self.requests[record.request_id]
        result = self.planner.plan(request, now=self.clock.now())
        if result.chosen is None:
            return None
        option = result.chosen
        events = [self._event(
            "JOURNEY_REPLANNED", "journey_commitment", record.journey_id,
            self._option_payload(option, record.request_id, {
                "affected_segments": [r.resource_ref for r in option.reservations],
                "reason": "升级后供给恢复或容量释放，获得可行方案，保障义务继续",
                "old_option_id": record.option_id,
                "rejected_options": self._rejected_summary(result.alternatives),
            }))]
        events.extend(self._reserve_events(
            record.journey_id, record.request_id, option, option.reservations))
        events.append(self._event(
            "JOURNEY_LIFECYCLED", "journey_commitment", record.journey_id,
            {"node": "CONFIRMED", "at_time": self.clock.now().isoformat(),
             "replanned": True}))
        committed = self._commit(events)
        return committed

    # ================================================================
    # 查询
    # ================================================================

    def journeys_for_request(self, request_id: str) -> list[JourneyRecord]:
        return [j for j in self.journeys.values() if j.request_id == request_id]

    def open_obligations(self) -> list[JourneyRecord]:
        return [j for j in self.journeys.values() if j.open]

    def quarantined(self) -> list[Quarantine]:
        return list(self._quarantine.values())

    # ================================================================
    # 事件投影与恢复
    # ================================================================

    def _replay(self) -> None:
        for event in self.store.stream():
            self._apply(event, replay=True)

    def _apply(self, event: Event, *, replay: bool = False) -> None:
        self._seq = max(self._seq, _seq_of(event.event_id))
        p = event.payload
        kind = event.event_type

        if kind == "REQUEST_ACCEPTED":
            request = TravelRequest(
                request_id=event.aggregate_id,
                business_ref=p["business_ref"], notice_id=p["notice_id"],
                person_ids=tuple(p["person_ids"]),
                role=TravelerRole(p["traveler_role"]),
                party_size=p["party_size"],
                wheelchair_count=p["wheelchair_count"], medical=p["medical"],
                equipment_units=p["equipment_units"],
                origin_lodging_id=p["origin_lodging_id"], duty_id=p["duty_id"],
                required_arrival=_dt(p["required_arrival"]),
                declared_at=_dt(p["declared_at"]), note=p.get("note", ""))
            self.requests[request.request_id] = request
            self._by_notice[request.notice_id] = request.request_id
            self._business.setdefault(request.business_ref, {})[request.signature()] = request.request_id

        elif kind == "REQUEST_QUARANTINED":
            incoming = TravelRequest(
                request_id=event.aggregate_id,
                business_ref=p["business_ref"], notice_id=p["notice_id"],
                person_ids=tuple(p["person_ids"]),
                role=TravelerRole(p["traveler_role"]),
                party_size=p["party_size"],
                wheelchair_count=p["wheelchair_count"], medical=p["medical"],
                equipment_units=p["equipment_units"],
                origin_lodging_id=p["origin_lodging_id"], duty_id=p["duty_id"],
                required_arrival=_dt(p["required_arrival"]),
                declared_at=_dt(p["declared_at"]), note=p.get("note", ""))
            self._quarantine[event.aggregate_id] = Quarantine(
                event.aggregate_id, p["business_ref"], p["conflict_fields"],
                p["existing_request_id"], p.get("detail", ""), incoming)
            self._by_notice[p["notice_id"]] = event.aggregate_id

        elif kind == "RESOURCE_RESERVED":
            self.inventory.acquire([Hold(
                p["resource_ref"], p["kind"], _dt(p["start"]), _dt(p["end"]),
                p["units"], p["request_id"], p["capacity"])])

        elif kind == "RESOURCE_RELEASED":
            self.inventory.release(p["request_id"], [p["resource_ref"]])

        elif kind in ("JOURNEY_PLANNED", "JOURNEY_REPLANNED"):
            record = self.journeys.get(event.aggregate_id)
            if record is None:
                record = JourneyRecord(journey_id=event.aggregate_id,
                                       request_id=p["request_id"])
                self.journeys[record.journey_id] = record
            if p.get("option_id"):
                record.option_id = p["option_id"]
                record.mode = p["mode"]
                record.label = p.get("label", "")
                record.depart_at = _dt(p["depart_at"])
                record.arrive_at = _dt(p["arrive_at"])
                record.cost = p["cost"]
                record.reservations = [
                    PlannedReservation.from_payload(r)
                    for r in p.get("reservations", [])
                ]
                record.reasons = list(p.get("reasons", ()))
                record.waiting_due = record.depart_at - self.wait_lead
                record.no_show_deadline = record.depart_at + self.no_show_grace
                record.complete_at = record.arrive_at
                record.escalation_reasons = []
            if kind == "JOURNEY_REPLANNED":
                record.changes.append({
                    "reason": p["reason"], "event_id": event.event_id,
                    "option_id": p.get("option_id", ""),
                    "affected_segments": p.get("affected_segments", []),
                })
            record.basis_event_ids.append(event.event_id)

        elif kind == "JOURNEY_LIFECYCLED":
            record = self.journeys[event.aggregate_id]
            record.node = p["node"]
            if p["node"] == "ESCALATED":
                record.reservations = []
                record.escalation_reasons = list(p.get("reasons", ()))

        elif kind == "COMMITMENT_CLOSED":
            record = self.journeys[event.aggregate_id]
            record.closed_outcome = p["outcome"]
            record.node = "COMPLETED" if p["outcome"] == "completed" else "NO_SHOW"

        elif kind == "SUPPLY_CHANGED":
            change_kind = p["change_kind"]
            target = p["affected_refs"][0].split(":", 1)[1]
            offline_now = not p.get("restored", False)
            if change_kind == "transit_suspension":
                self.registry.set_trip_suspended(target, offline_now)
            elif change_kind == "road_restriction":
                self.registry.set_segment_restricted(target, offline_now)
            elif change_kind == "vehicle_offline":
                self.registry.set_vehicle_offline(target, offline_now)
            elif change_kind == "security_window_closed":
                self.registry.set_window_closed(target, offline_now)

        elif kind == "SCHEDULE_CHANGED":
            self.registry.reschedule_duty(
                p["duty_id"], _dt(p["new_ready_at"]),
                tuple(p.get("window_ids") or ()))


def _seq_of(event_id: str) -> int:
    try:
        return int(event_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0
