"""审计 API：回答三个必须能解释的问题。

1. 某人为何采用当前路线（why）；
2. 资源从何处扣减、当前被谁占用（ledger）；
3. 某次变更之后哪些安排被释放、被什么替代（impact）。

审计视图只依赖事件日志本身，重放即可工作，进程崩溃后也能直接对日志取证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .events import Event


def _dt(value: Any) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


@dataclass
class RequestView:
    request_id: str
    business_ref: str
    notice_id: str
    person_ids: list[str]
    role: str
    party_size: int
    wheelchair_count: int
    medical: bool
    equipment_units: int
    origin_lodging_id: str
    duty_id: str
    required_arrival: datetime


@dataclass
class LedgerEntry:
    event_id: str
    at: datetime
    resource_ref: str
    kind: str
    delta: int                       # 正为扣减，负为释放
    units: int
    capacity: int
    journey_id: str
    request_id: str
    reason: str
    trigger_event_id: str | None = None


@dataclass
class JourneyView:
    journey_id: str
    request_id: str
    node: str = "CONFIRMED"
    option_id: str = ""
    mode: str = ""
    label: str = ""
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    cost: int = 0
    reasons: list[str] = field(default_factory=list)
    rejected_options: list[dict[str, Any]] = field(default_factory=list)
    reservations: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    closed_outcome: str | None = None
    closed_at: datetime | None = None
    basis_event_ids: list[str] = field(default_factory=list)
    plan_event_id: str = ""

    @property
    def open(self) -> bool:
        return self.closed_outcome is None


@dataclass
class QuarantineView:
    request_id: str
    business_ref: str
    conflict_fields: list[str]
    existing_request_id: str
    detail: str


@dataclass
class WhyReport:
    subject: str
    request: RequestView | None
    journey: JourneyView | None
    quarantine: QuarantineView | None = None

    @property
    def found(self) -> bool:
        return self.request is not None or self.quarantine is not None


@dataclass
class ImpactItem:
    journey_id: str
    request_id: str
    kind: str                        # replaced / escalated
    reason: str
    released_refs: list[str]
    new_option_id: str = ""
    new_mode: str = ""
    new_label: str = ""
    new_depart_at: datetime | None = None
    new_arrive_at: datetime | None = None


@dataclass
class ImpactReport:
    trigger_event_id: str
    trigger: Event | None
    items: list[ImpactItem] = field(default_factory=list)
    untouched_completed: list[str] = field(default_factory=list)

    @property
    def affected_count(self) -> int:
        return len(self.items)


class AuditView:
    """从事件流重放出的只读审计投影。"""

    def __init__(self, events: Iterable[Event]) -> None:
        self.events: list[Event] = list(events)
        self.requests: dict[str, RequestView] = {}
        self.journeys: dict[str, JourneyView] = {}
        self.quarantine: dict[str, QuarantineView] = {}
        self.ledger: list[LedgerEntry] = []
        self._person_index: dict[str, str] = {}
        self._business_index: dict[str, str] = {}
        for event in self.events:
            self._apply(event)

    # -- 投影 -------------------------------------------------------------

    def _apply(self, event: Event) -> None:
        p = event.payload
        kind = event.event_type

        if kind in ("REQUEST_ACCEPTED", "REQUEST_QUARANTINED"):
            view = RequestView(
                request_id=event.aggregate_id,
                business_ref=p["business_ref"], notice_id=p["notice_id"],
                person_ids=list(p["person_ids"]), role=p["traveler_role"],
                party_size=p["party_size"], wheelchair_count=p["wheelchair_count"],
                medical=p["medical"], equipment_units=p["equipment_units"],
                origin_lodging_id=p["origin_lodging_id"], duty_id=p["duty_id"],
                required_arrival=_dt(p["required_arrival"]))
            if kind == "REQUEST_QUARANTINED":
                self.quarantine[event.aggregate_id] = QuarantineView(
                    event.aggregate_id, p["business_ref"], p["conflict_fields"],
                    p["existing_request_id"], p.get("detail", ""))
            else:
                self.requests[event.aggregate_id] = view
                for person in view.person_ids:
                    self._person_index[person] = view.request_id
                self._business_index[view.business_ref] = view.request_id

        elif kind == "RESOURCE_RESERVED":
            self.ledger.append(LedgerEntry(
                event_id=event.event_id, at=event.occurred_at,
                resource_ref=p["resource_ref"], kind=p["kind"], delta=p["units"],
                units=p["units"], capacity=p["capacity"],
                journey_id=p["journey_id"], request_id=p["request_id"],
                reason=p.get("mode", "占用")))

        elif kind == "RESOURCE_RELEASED":
            self.ledger.append(LedgerEntry(
                event_id=event.event_id, at=event.occurred_at,
                resource_ref=p["resource_ref"], kind=p["kind"], delta=-p["units"],
                units=p["units"], capacity=p["capacity"],
                journey_id=p["journey_id"], request_id=p["request_id"],
                reason=p.get("reason", "释放"),
                trigger_event_id=p.get("trigger_event_id")))

        elif kind in ("JOURNEY_PLANNED", "JOURNEY_REPLANNED"):
            journey = self.journeys.get(event.aggregate_id)
            if journey is None:
                journey = JourneyView(journey_id=event.aggregate_id,
                                      request_id=p["request_id"])
                self.journeys[journey.journey_id] = journey
            if not journey.plan_event_id:
                journey.plan_event_id = event.event_id
            if p.get("option_id"):
                journey.option_id = p["option_id"]
                journey.mode = p["mode"]
                journey.label = p.get("label", "")
                journey.depart_at = _dt(p["depart_at"])
                journey.arrive_at = _dt(p["arrive_at"])
                journey.cost = p["cost"]
                journey.reasons = list(p.get("reasons", ()))
                journey.reservations = list(p.get("reservations", ()))
                journey.escalation_reasons = []
            if p.get("rejected_options"):
                journey.rejected_options = list(p["rejected_options"])
            if kind == "JOURNEY_REPLANNED":
                journey.changes.append({
                    "event_id": event.event_id,
                    "reason": p.get("reason", ""),
                    "old_option_id": p.get("old_option_id", ""),
                    "option_id": p.get("option_id", ""),
                    "affected_segments": p.get("affected_segments", []),
                    "trigger_event_id": p.get("trigger_event_id"),
                })
            journey.basis_event_ids.append(event.event_id)

        elif kind == "JOURNEY_LIFECYCLED":
            journey = self.journeys[event.aggregate_id]
            journey.node = p["node"]
            if p["node"] == "ESCALATED":
                journey.escalation_reasons = list(p.get("reasons", ()))
                journey.reservations = []

        elif kind == "COMMITMENT_CLOSED":
            journey = self.journeys[event.aggregate_id]
            journey.closed_outcome = p["outcome"]
            journey.node = "COMPLETED" if p["outcome"] == "completed" else "NO_SHOW"
            journey.closed_at = _dt(p["closed_at"])
            journey.basis_event_ids = list(p.get("basis_event_ids", journey.basis_event_ids))

    # -- 查询 -------------------------------------------------------------

    def resolve_request(self, subject: str) -> str | None:
        """接受人员编号、申报编号、行程编号或业务编号作为查询主体。"""
        if subject in self.requests:
            return subject
        if subject in self._person_index:
            return self._person_index[subject]
        if subject in self._business_index:
            return self._business_index[subject]
        if subject.startswith("J-") and subject[2:] in self.requests:
            return subject[2:]
        journey = self.journeys.get(subject)
        if journey is not None:
            return journey.request_id
        return None

    def why(self, subject: str) -> WhyReport:
        request_id = self.resolve_request(subject)
        if request_id is None:
            quarantine = next((q for q in self.quarantine.values()
                               if subject in (q.request_id, q.business_ref)), None)
            return WhyReport(subject=subject, request=None, journey=None,
                             quarantine=quarantine)
        journey = next((j for j in self.journeys.values()
                        if j.request_id == request_id), None)
        return WhyReport(subject=subject, request=self.requests[request_id],
                         journey=journey)

    def balances(self) -> dict[str, dict[str, Any]]:
        """按资源汇总当前净扣减与占用者。"""
        result: dict[str, dict[str, Any]] = {}
        for entry in self.ledger:
            slot = result.setdefault(entry.resource_ref, {
                "kind": entry.kind, "capacity": entry.capacity,
                "used": 0, "holders": {}, "last_event": entry.event_id,
            })
            slot["used"] += entry.delta
            slot["last_event"] = entry.event_id
            holders = slot["holders"]
            holders[entry.journey_id] = holders.get(entry.journey_id, 0) + entry.delta
            if holders[entry.journey_id] == 0:
                del holders[entry.journey_id]
        return result

    def ledger_for(self, resource_ref: str | None = None) -> list[LedgerEntry]:
        if resource_ref is None:
            return list(self.ledger)
        return [e for e in self.ledger if e.resource_ref == resource_ref]

    def current_holders(self, resource_ref: str) -> list[tuple[str, int]]:
        balances = self.balances().get(resource_ref)
        if not balances:
            return []
        return sorted(balances["holders"].items())

    def impact(self, trigger_event_id: str) -> ImpactReport:
        """沿 trigger_event_id 血缘找出被释放、替代或升级的全部安排。"""
        trigger = next((e for e in self.events if e.event_id == trigger_event_id), None)
        report = ImpactReport(trigger_event_id=trigger_event_id, trigger=trigger)
        for event in self.events:
            if event.event_type == "JOURNEY_REPLANNED" and \
                    event.payload.get("trigger_event_id") == trigger_event_id:
                p = event.payload
                released = self._released_by_trigger(trigger_event_id,
                                                     event.aggregate_id)
                report.items.append(ImpactItem(
                    journey_id=event.aggregate_id,
                    request_id=p["request_id"], kind="replaced",
                    reason=p.get("reason", ""), released_refs=released,
                    new_option_id=p.get("option_id", ""),
                    new_mode=p.get("mode", ""), new_label=p.get("label", ""),
                    new_depart_at=_dt(p["depart_at"]),
                    new_arrive_at=_dt(p["arrive_at"])))
            elif event.event_type == "JOURNEY_LIFECYCLED" and \
                    event.payload.get("node") == "ESCALATED" and \
                    event.payload.get("trigger_event_id") == trigger_event_id:
                released = self._released_by_trigger(trigger_event_id,
                                                     event.aggregate_id)
                request_id = self.journeys[event.aggregate_id].request_id
                report.items.append(ImpactItem(
                    journey_id=event.aggregate_id, request_id=request_id,
                    kind="escalated", reason=event.payload.get("reason", ""),
                    released_refs=released))
        if trigger is not None:
            # 以触发事件当时为准：那时已经闭环的转运保留当时依据
            report.untouched_completed = sorted(
                j.journey_id for j in self.journeys.values()
                if j.closed_at is not None and j.closed_at <= trigger.occurred_at)
        return report

    def _released_by_trigger(self, trigger_event_id: str,
                             journey_id: str) -> list[str]:
        return [e.resource_ref for e in self.ledger
                if e.trigger_event_id == trigger_event_id
                and e.journey_id == journey_id and e.delta < 0]
