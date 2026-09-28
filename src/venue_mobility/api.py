"""保障服务的 JSON 命令面。

任何接入方（HTTP 处理器、批处理、CLI）都只与 :meth:`ServiceHub.dispatch` 交互：
入参为 ``{"command": ..., "params": {...}}``，出参为 JSON 可序列化字典；
业务拒绝统一返回 ``{"error": 代码, "message": 中文说明}``，不抛到边界之外。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .model import Scenario
from .service import MobilityService, ServiceError


class ServiceHub:
    def __init__(self, scenario: Scenario, store: str | Path | None = None, start_at: str | None = None) -> None:
        self.scenario = scenario
        self.store = Path(store) if store else None
        if self.store is not None and self.store.exists():
            self.service = MobilityService.from_store(self.store, scenario, start_at=start_at)
        else:
            self.service = MobilityService(scenario, start_at=start_at or "2026-09-25T06:00:00+08:00")

    @classmethod
    def from_files(cls, scenario_path: str | Path, store: str | Path | None = None) -> "ServiceHub":
        import json

        data = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
        return cls(Scenario.from_dict(data), store=store)

    def dispatch(self, command: Mapping[str, Any]) -> dict[str, Any]:
        name = command.get("command")
        params = command.get("params") or {}
        service = self.service
        try:
            if name == "submit":
                result = service.submit_request(params)
            elif name == "resolve_review":
                result = service.resolve_review(params["business_ref"], bool(params.get("accept", True)))
            elif name == "confirm":
                result = service.confirm(params["business_ref"], params.get("option_id"))
            elif name == "begin_waiting":
                result = service.begin_waiting(params["business_ref"], int(params.get("minutes", 15)))
            elif name == "complete":
                result = service.complete_transfer(params["business_ref"])
            elif name == "accept_upgrade":
                result = service.accept_upgrade(params["business_ref"])
            elif name == "advance":
                result = service.advance(int(params.get("minutes", 1)))
            elif name == "tick":
                result = service.tick()
            elif name == "suspend_transit":
                result = service.suspend_transit(params["transit_id"], params.get("reason", "transit_suspended"))
            elif name == "impose_closure":
                result = service.impose_closure(params["closure"], params.get("reason", "route_closed"))
            elif name == "reschedule_session":
                result = service.reschedule_session(
                    params["session_id"],
                    params["start_at"],
                    params["latest_arrival_at"],
                    params.get("reason", "session_rescheduled"),
                )
            elif name == "lift_closure":
                result = service.lift_closure(params["closure_id"], params.get("reason", "route_reopened"))
            elif name == "restore_transit":
                result = service.restore_transit(params["transit_id"], params.get("reason", "transit_restored"))
            elif name == "explain":
                result = service.explain(params["ref"])
            elif name == "ledger":
                result = service.resource_ledger()
            elif name == "changes":
                result = {"changes": service.changes()}
            elif name == "pending":
                result = {"obligations": service.pending_obligations(), "now": service.clock.now.isoformat()}
            elif name == "events":
                result = {"events": list(service.events)}
            elif name == "snapshot":
                result = self.snapshot()
            else:
                return {"error": "unknown_command", "message": f"未知命令：{name}"}
        except ServiceError as exc:
            return {"error": "service_rule", "message": str(exc), "command": name}
        except KeyError as exc:
            return {"error": "missing_param", "message": f"缺少参数：{exc.args[0]}", "command": name}
        if self.store is not None:
            service.save(self.store)
        return {"ok": True, "command": name, "result": result}

    def run_script(self, commands: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self.dispatch(command) for command in commands]

    def snapshot(self) -> dict[str, Any]:
        return {
            "now": self.service.clock.now.isoformat(),
            "journeys": {
                ref: {
                    "status": state.status,
                    "person_id": state.request.person_id,
                    "role": state.request.role,
                    "need_tier": state.request.need_tier,
                    "option": state.basis_option or (state.current.option if state.current else None),
                    "expires_at": state.expires_at,
                    "upgrade_due_at": state.upgrade_due_at,
                }
                for ref, state in self.service.journeys.items()
            },
            "pending": self.service.pending_obligations(),
        }
