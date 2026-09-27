"""赛事保障中心管理命令。

子命令：

* ``demo``    运行可复现联调场景（申报、核查、停运改派、安检升级与恢复、
              候车/登乘/失约/完成、管制后已完成行程保留依据），写事件日志；
* ``events``  按顺序列出事件日志；
* ``why``     解释某人（人员编号/申报号/行程号/业务编号）为何采用当前路线；
* ``ledger``  资源扣减台账，可按资源过滤；
* ``impact``  给出某次变更事件释放或替代了哪些后续安排。

审计类命令只依赖事件日志，不读主数据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditView
from .events import Clock, Event, EventStore
from .scenario import TZ, at, build_scenario
from .service import MobilityService


# ============================================================
# 演示场景
# ============================================================

def run_demo(journal_path: str) -> MobilityService:
    scenario = build_scenario()
    journal = Path(journal_path)
    if journal.exists():
        # 演示日志每次重跑重建；生产恢复走直接打开既有日志的路径
        journal.unlink()
    store = EventStore(journal)
    clock = Clock(at(6, 0))
    svc = MobilityService(scenario.registry, store, clock)

    def say(line: str) -> None:
        print(line)

    say("== 06:00 受理 4 份申报 ==")
    svc.submit_many(scenario.requests)
    for request in scenario.requests:
        journey = svc.journeys[f"J-{request.request_id}"]
        hard = "（硬需求）" if request.hard_need else ""
        say(f"  {request.request_id} {request.role.value}{hard}: "
            f"{journey.mode or '升级中'} {journey.label} → "
            f"{journey.option_id or '—'}")

    say("== 重复通知（同 notice-0001）再次送达 ==")
    svc.submit(scenario.duplicate)
    say(f"  行程数仍为 {len(svc.journeys)}，未重复派车")

    say("== 同业务编号 BIZ-1001 改时间的通知送达 ==")
    svc.submit(scenario.quarrel)
    for q in svc.quarantined():
        say(f"  {q.request_id} 进入核查：与 {q.existing_request_id} 的"
            f"{'、'.join(q.conflict_fields)}不一致，不派车")

    say("== 06:20 地铁3号线 metro-3 停运 ==")
    svc.clock.advance_to(at(6, 20))
    svc.suspend_trip("metro-3")
    for rid in ("req-swim-01", "req-swim-02", "req-media-01"):
        journey = svc.journeys[f"J-{rid}"]
        say(f"  {rid} 改乘 {journey.mode} {journey.label}（{journey.option_id}）")

    say("== 06:35 无障碍安检窗口 W-AQU-2 关闭 ==")
    svc.clock.advance_to(at(6, 35))
    svc.close_security_window("W-AQU-2")
    j_wheel = svc.journeys["J-req-swim-02"]
    say(f"  req-swim-02 节点={j_wheel.node}：普通窗口不能覆盖无障碍硬需求，升级")
    for reason in j_wheel.escalation_reasons[:2]:
        say(f"    - {reason}")

    say("== 06:40 W-AQU-2 重新开放 ==")
    svc.clock.advance_to(at(6, 40))
    svc.reopen_security_window("W-AQU-2")
    say(f"  req-swim-02 节点恢复={j_wheel.node}，方案 {j_wheel.option_id}，保障义务续办")

    say("== 06:45 人工核查结论：以原申报为准 ==")
    svc.clock.advance_to(at(6, 45))
    svc.resolve_quarantine("req-swim-01-change", use_incoming=False)
    say("  晚场通知作废，既有行程不动")

    say("== 08:00 进入候车；08:10 三组登乘，轮椅组未登乘 ==")
    svc.clock.advance_to(at(8, 0))
    svc.tick()
    svc.clock.advance_to(at(8, 10))
    for rid in ("req-swim-01", "req-swim-03", "req-media-01"):
        svc.board(f"J-{rid}")
        say(f"  {rid} 已登乘")

    say("== 08:25 轮椅组超过宽限未登乘，按失约处理并释放资源 ==")
    svc.clock.advance_to(at(8, 25))
    svc.tick()
    released = [e.payload["resource_ref"] for e in store.stream()
                if e.event_type == "RESOURCE_RELEASED"
                and e.payload["request_id"] == "req-swim-02"]
    say(f"  节点={j_wheel.node}，释放：{', '.join(sorted(set(released)))}")

    say("== 08:40 登乘各组按时到达闭环 ==")
    svc.clock.advance_to(at(8, 40))
    svc.tick()
    for rid in ("req-swim-01", "req-swim-03", "req-media-01"):
        journey = svc.journeys[f"J-{rid}"]
        say(f"  {rid} {journey.closed_outcome}，依据事件 {journey.basis_event_ids}")

    say("== 08:50 road-AB 临时管制：已完成转运不回溯 ==")
    svc.clock.advance_to(at(8, 50))
    svc.restrict_road("road-AB")
    completed = [j.journey_id for j in svc.journeys.values()
                 if j.closed_outcome == "completed"]
    say(f"  开放行程数 {len(svc.open_obligations())}；已完成保留：{', '.join(sorted(completed))}")
    svc.clock.advance_to(at(8, 55))
    svc.lift_road_restriction("road-AB")

    say(f"== 共 {len(store)} 个事件写入 {journal_path} ==")
    return svc


# ============================================================
# 输出
# ============================================================

def _load_view(journal_path: str) -> AuditView:
    store = EventStore(journal_path)
    return AuditView(store.stream())


def cmd_events(args: argparse.Namespace) -> int:
    for event in EventStore(args.journal).stream():
        print(f"{event.occurred_at:%H:%M} v{event.version} {event.event_id} "
              f"{event.event_type} {event.aggregate_type}/{event.aggregate_id}")
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    view = _load_view(args.journal)
    report = view.why(args.subject)
    if not report.found:
        print(f"未找到与 {args.subject} 相关的申报或行程")
        return 1
    if report.quarantine is not None:
        q = report.quarantine
        print(f"{q.request_id} 已进入核查（业务编号 {q.business_ref}）")
        print(f"  冲突字段：{'、'.join(q.conflict_fields)}")
        print(f"  对照申报：{q.existing_request_id}")
        print(f"  说明：{q.detail}")
        return 0
    request = report.request
    journey = report.journey
    assert request is not None
    print(f"申报 {request.request_id}（业务编号 {request.business_ref}，"
          f"通知 {request.notice_id}）")
    print(f"  人员：{', '.join(request.person_ids)}；角色：{request.role}；"
          f"{request.party_size} 人；轮椅 {request.wheelchair_count}；"
          f"医疗随护 {request.medical}；器材 {request.equipment_units}")
    print(f"  {request.origin_lodging_id} → {request.duty_id}，"
          f"要求到场 {request.required_arrival:%H:%M}")
    if journey is None:
        print("  尚未形成行程")
        return 0
    print(f"  当前节点：{journey.node}；闭环：{journey.closed_outcome or '进行中'}")
    if journey.option_id:
        print(f"  当前方案：{journey.mode} {journey.label}（{journey.option_id}），"
              f"{journey.depart_at:%H:%M} 出发 / {journey.arrive_at:%H:%M} 到达，"
              f"成本分 {journey.cost}")
    print("  采用理由：")
    for reason in journey.reasons:
        print(f"    - {reason}")
    if journey.changes:
        print("  变更链：")
        for change in journey.changes:
            print(f"    - {change['event_id']}：{change['reason']}；"
                  f"{change['old_option_id']} → {change['option_id']}")
    if journey.escalation_reasons:
        print("  升级原因：")
        for reason in journey.escalation_reasons:
            print(f"    - {reason}")
    if journey.reservations:
        print("  资源扣减：")
        for reservation in journey.reservations:
            print(f"    - {reservation['resource_ref']} 占用 {reservation['units']} "
                  f"/ 容量 {reservation['capacity']}（{reservation['kind']}）")
    print(f"  依据事件：{', '.join(journey.basis_event_ids)}")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    view = _load_view(args.journal)
    entries = view.ledger_for(args.resource)
    if not entries:
        print("没有匹配的台账记录")
        return 1
    for entry in entries:
        sign = "扣减" if entry.delta > 0 else "释放"
        line = (f"{entry.at:%H:%M} {entry.event_id} {sign} {entry.resource_ref} "
                f"{abs(entry.delta)}（容量 {entry.capacity}）"
                f" 行程 {entry.journey_id}：{entry.reason}")
        if entry.trigger_event_id:
            line += f" ← 触发 {entry.trigger_event_id}"
        print(line)
    balances = view.balances()
    print("-- 当前净占用 --")
    for ref in sorted({e.resource_ref for e in entries}):
        slot = balances[ref]
        holders = ", ".join(f"{jid}:{units}" for jid, units in slot["holders"].items()) or "无"
        print(f"{ref}：{slot['used']}/{slot['capacity']}，占用者 {holders}")
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    view = _load_view(args.journal)
    report = view.impact(args.trigger_event)
    if report.trigger is None:
        print(f"未找到事件 {args.trigger_event}")
        return 1
    trigger = report.trigger
    print(f"触发事件 {trigger.event_id} {trigger.event_type}："
          f"{trigger.payload.get('reason', '')}")
    if not report.items:
        print("没有任何后续安排被改变")
    for item in report.items:
        if item.kind == "replaced":
            print(f"  替代 {item.journey_id}（{item.request_id}）：{item.reason}")
            print(f"    释放：{', '.join(item.released_refs) or '（无）'}")
            print(f"    新方案：{item.new_mode} {item.new_label}（{item.new_option_id}），"
                  f"{item.new_depart_at:%H:%M} 出发 / {item.new_arrive_at:%H:%M} 到达")
        else:
            print(f"  升级 {item.journey_id}（{item.request_id}）：{item.reason}")
            print(f"    释放：{', '.join(item.released_refs) or '（无）'}")
    print(f"  已完成、保留当时依据的转运："
          f"{', '.join(report.untouched_completed) or '无'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="venue_mobility.console",
                                     description="分散赛区通勤保障决策台")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="运行联调场景并写事件日志")
    demo.add_argument("--journal", default="data/demo-events.jsonl")
    demo.set_defaults(func=lambda a: (run_demo(a.journal), 0)[1])

    events = sub.add_parser("events", help="列出事件日志")
    events.add_argument("--journal", default="data/demo-events.jsonl")
    events.set_defaults(func=cmd_events)

    why = sub.add_parser("why", help="解释某人的当前路线")
    why.add_argument("subject")
    why.add_argument("--journal", default="data/demo-events.jsonl")
    why.set_defaults(func=cmd_why)

    ledger = sub.add_parser("ledger", help="资源扣减台账")
    ledger.add_argument("--resource", default=None)
    ledger.add_argument("--journal", default="data/demo-events.jsonl")
    ledger.set_defaults(func=cmd_ledger)

    impact = sub.add_parser("impact", help="查看变更释放/替代的安排")
    impact.add_argument("trigger_event")
    impact.add_argument("--journal", default="data/demo-events.jsonl")
    impact.set_defaults(func=cmd_impact)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
