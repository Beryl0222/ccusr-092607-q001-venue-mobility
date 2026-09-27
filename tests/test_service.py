import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from venue_mobility.audit import AuditView
from venue_mobility.events import Clock, EventStore
from venue_mobility.model import TravelRequest, TravelerRole
from venue_mobility.scenario import TZ, at, build_scenario
from venue_mobility.service import MobilityService, IllegalTransition


class DemoTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "events.jsonl"
        scenario = build_scenario()
        self.scenario = scenario
        self.clock = Clock(at(6, 0))
        self.store = EventStore(self.journal)
        self.svc = MobilityService(scenario.registry, self.store, self.clock)
        self.svc.submit_many(scenario.requests)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def journey(self, request_id: str):
        return self.svc.journeys[f"J-{request_id}"]


class SubmissionTests(DemoTestBase):
    def test_initial_plans(self) -> None:
        self.assertEqual("transit", self.journey("req-swim-01").mode)
        self.assertEqual("dedicated", self.journey("req-swim-03").mode)

    def test_duplicate_notice_does_not_dispatch_twice(self) -> None:
        events_before = len(self.store)
        journeys_before = len(self.svc.journeys)
        self.svc.submit(self.scenario.duplicate)
        self.assertEqual(journeys_before, len(self.svc.journeys))
        self.assertEqual(events_before, len(self.store))

    def test_same_signature_new_notice_is_also_idempotent(self) -> None:
        twin = TravelRequest(
            "req-twin", "BIZ-1001", "notice-7777",
            ("athlete-02", "athlete-01"), TravelerRole.ATHLETE,
            party_size=2, wheelchair_count=0, medical=False, equipment_units=1,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6, 6))
        events_before = len(self.store)
        self.svc.submit(twin)
        self.assertEqual(events_before, len(self.store))
        self.assertNotIn("J-req-twin", self.svc.journeys)

    def test_same_business_ref_different_time_is_quarantined(self) -> None:
        self.svc.submit(self.scenario.quarrel)
        quarantined = self.svc.quarantined()
        self.assertEqual(1, len(quarantined))
        self.assertEqual(["时间"], quarantined[0].conflict_fields)
        self.assertNotIn("J-req-swim-01-change", self.svc.journeys)

    def test_quarantine_reject_incoming_keeps_old_plan(self) -> None:
        self.svc.submit(self.scenario.quarrel)
        old_option = self.journey("req-swim-01").option_id
        self.svc.clock.advance_to(at(6, 45))
        self.svc.resolve_quarantine("req-swim-01-change", use_incoming=False)
        self.assertEqual([], self.svc.quarantined())
        self.assertEqual(old_option, self.journey("req-swim-01").option_id)

    def test_quarantine_accept_incoming_cancels_old_and_dispatches_new(self) -> None:
        later = TravelRequest(
            "req-swim-01-change", "BIZ-1001", "notice-0099",
            ("athlete-01", "athlete-02"), TravelerRole.ATHLETE,
            party_size=2, wheelchair_count=0, medical=False, equipment_units=1,
            origin_lodging_id="L-VIL", duty_id="D-GYM",  # 路线也变了
            required_arrival=at(10, 40), declared_at=at(6, 10))
        self.svc.submit(later)
        q = self.svc.quarantined()[0]
        self.assertIn("路线", q.conflict_fields)
        self.svc.resolve_quarantine("req-swim-01-change", use_incoming=True)
        old = self.journey("req-swim-01")
        self.assertEqual("cancelled", old.closed_outcome)
        new = self.journey("req-swim-01-change")
        self.assertIsNone(new.closed_outcome)
        self.assertTrue(new.option_id)


class LifecycleTests(DemoTestBase):
    def test_waiting_boarding_completion(self) -> None:
        journey = self.journey("req-swim-01")
        self.clock.advance_to(journey.waiting_due)
        self.svc.tick()
        self.assertEqual("WAITING", journey.node)
        self.clock.advance_to(journey.depart_at)
        self.svc.board(journey.journey_id)
        self.assertEqual("BOARDED", journey.node)
        self.clock.advance_to(journey.complete_at)
        self.svc.tick()
        self.assertEqual("completed", journey.closed_outcome)
        # 完成即到期释放，容量回归台账
        balances = AuditView(self.store.stream()).balances()
        for ref in set(journey.resource_refs()):
            self.assertEqual(0, balances[ref]["used"])

    def test_boarding_requires_waiting_node(self) -> None:
        with self.assertRaises(IllegalTransition):
            self.svc.board("J-req-swim-01")

    def test_no_show_releases_resources_and_closes(self) -> None:
        journey = self.journey("req-swim-01")
        refs = set(journey.resource_refs())
        self.clock.advance_to(journey.no_show_deadline)
        self.svc.tick()
        self.assertEqual("cancelled", journey.closed_outcome)
        self.assertEqual("NO_SHOW", journey.node)
        balances = AuditView(self.store.stream()).balances()
        for ref in refs:
            self.assertEqual(0, balances[ref]["used"])

    def test_tick_is_idempotent(self) -> None:
        journey = self.journey("req-swim-01")
        self.clock.advance_to(journey.no_show_deadline)
        self.svc.tick()
        n_after_first = len(self.store)
        self.svc.tick()
        self.svc.tick()
        self.assertEqual(n_after_first, len(self.store))


class ReplanningTests(DemoTestBase):
    def test_suspension_only_replans_open_transit_journeys(self) -> None:
        medical_before = self.journey("req-swim-03").option_id
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")
        self.assertEqual("shuttle", self.journey("req-swim-01").mode)
        self.assertEqual("shuttle", self.journey("req-media-01").mode)
        # 专车行程不经地铁，不被重排
        self.assertEqual(medical_before, self.journey("req-swim-03").option_id)
        self.assertEqual(1, len(self.journey("req-swim-01").changes))

    def test_completed_transfers_are_never_rewritten(self) -> None:
        journey = self.journey("req-swim-01")
        self.clock.advance_to(journey.waiting_due)
        self.svc.tick()
        self.clock.advance_to(journey.depart_at)
        self.svc.board(journey.journey_id)
        self.clock.advance_to(journey.complete_at)
        self.svc.tick()
        basis = list(journey.basis_event_ids)
        self.clock.advance_to(at(8, 50))
        self.svc.suspend_trip("metro-3")
        self.svc.restrict_road("road-AB")
        self.svc.restrict_road("road-BC")
        self.assertEqual("completed", journey.closed_outcome)
        self.assertEqual(basis, journey.basis_event_ids)
        self.assertEqual([], journey.changes)

    def test_window_closure_escalates_hard_needs_and_recovers(self) -> None:
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")
        wheel = self.journey("req-swim-02")
        self.clock.advance_to(at(6, 35))
        self.svc.close_security_window("W-AQU-2")
        self.assertEqual("ESCALATED", wheel.node)
        self.assertEqual([], wheel.resource_refs())
        self.assertTrue(any("无障碍" in r for r in wheel.escalation_reasons))
        self.clock.advance_to(at(6, 40))
        self.svc.reopen_security_window("W-AQU-2")
        self.assertEqual("CONFIRMED", wheel.node)
        self.assertTrue(wheel.option_id)
        self.assertGreater(len(wheel.resource_refs()), 0)

    def test_schedule_change_replans_open_journeys_and_adapts_again(self) -> None:
        # 临时改项把就绪时刻提前到 08:10：规划器必须倒推出更早的出发方案
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")  # 之后班车原方案约 08:20 到达
        old_arrival = self.journey("req-swim-01").arrive_at
        self.clock.advance_to(at(6, 25))
        self.svc.change_schedule("D-SWIM", at(8, 10))
        for rid in ("req-swim-01", "req-swim-02", "req-swim-03", "req-media-01"):
            journey = self.journey(rid)
            self.assertEqual("CONFIRMED", journey.node)
            self.assertLess(journey.arrive_at, old_arrival)
            self.assertTrue(any("改项" in c["reason"] for c in journey.changes))
        # 改期推迟回 09:00，方案再次适配
        self.clock.advance_to(at(6, 30))
        self.svc.change_schedule("D-SWIM", at(9, 0))
        for rid in ("req-swim-01", "req-swim-02", "req-swim-03", "req-media-01"):
            self.assertEqual("CONFIRMED", self.journey(rid).node, rid)

    def test_unrelated_restriction_does_not_replan(self) -> None:
        journey = self.journey("req-swim-03")  # 专车，A→C 经 AB、BC
        option_before = journey.option_id
        self.clock.advance_to(at(6, 20))
        # road-AD 通往分赛区 D，不在本场馆通路上：不得重排
        self.svc.restrict_road("road-AD")
        self.assertTrue(journey.open)
        self.assertEqual(option_before, journey.option_id)
        self.assertEqual([], journey.changes)

    def test_replan_releases_old_and_reserves_new_atomically(self) -> None:
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")
        view = AuditView(self.store.stream())
        balances = view.balances()
        # 旧地铁占用余额必须为 0
        self.assertEqual(0, balances["transit:metro-3:seat"]["used"])
        # 班车与窗口仍被行程净占用
        self.assertGreaterEqual(balances["vehicle:van-01:seat"]["used"], 1)


class ContentionTests(unittest.TestCase):
    def test_hard_need_outranks_ordinary_priority(self) -> None:
        scenario = build_scenario()
        # 关掉地铁与班车，只留一辆专车：医疗申报与普通媒体同时申报
        scenario.registry.set_trip_suspended("metro-3", True)
        scenario.registry.set_trip_suspended("metro-3-late", True)
        scenario.registry.set_vehicle_offline("van-01", True)
        scenario.registry.set_vehicle_offline("van-02", True)
        svc = MobilityService(scenario.registry, EventStore(), Clock(at(6)))
        media = TravelRequest(
            "req-c-media", "BIZ-CM", "n-cm", ("m1",), TravelerRole.MEDIA,
            party_size=1, wheelchair_count=0, medical=False, equipment_units=0,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6))
        medical = TravelRequest(
            "req-c-med", "BIZ-CX", "n-cx", ("x1",), TravelerRole.STAFF,
            party_size=1, wheelchair_count=0, medical=True, equipment_units=0,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6))
        # 媒体排在申报列表前面，医疗随后竞争；同批裁决必须由硬门槛而非送达顺序决定
        svc.submit_many([media, medical])
        # 媒体无法获得具医疗资质的专车，必然升级；医疗硬需求不被普通优先级挤出
        self.assertEqual("ESCALATED", svc.journeys["J-req-c-media"].node)
        self.assertEqual("CONFIRMED", svc.journeys["J-req-c-med"].node)
        self.assertEqual("dedicated-car-01", svc.journeys["J-req-c-med"].option_id)
        self.assertTrue(any(
            "整包占用" in r
            for r in svc.journeys["J-req-c-media"].escalation_reasons)
            or any("专车" in r for r in svc.journeys["J-req-c-media"].escalation_reasons))


class RecoveryTests(DemoTestBase):
    def test_replay_continues_unfinished_obligations(self) -> None:
        journey = self.journey("req-swim-01")
        # 进程在地铁行程发车宽限之后崩溃：恢复时应立即续办失约
        later = Clock(journey.no_show_deadline)
        recovered = MobilityService.restore(
            build_scenario().registry, self.store, later)
        replayed = recovered.journeys[journey.journey_id]
        self.assertEqual("NO_SHOW", replayed.node)
        self.assertEqual("cancelled", replayed.closed_outcome)
        # 同批地铁行程（含媒体组）一并失约；更晚出发的医疗专车此时仍开放待续办
        self.assertEqual("NO_SHOW", recovered.journeys["J-req-swim-02"].node)
        self.assertEqual("NO_SHOW", recovered.journeys["J-req-media-01"].node)
        still_open = {j.request_id for j in recovered.open_obligations()}
        self.assertEqual({"req-swim-03"}, still_open)

    def test_replay_state_matches_live_state(self) -> None:
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")
        recovered = MobilityService(
            build_scenario().registry, self.store, Clock(at(6, 20)))
        for journey_id, journey in self.svc.journeys.items():
            other = recovered.journeys[journey_id]
            self.assertEqual(journey.node, other.node)
            self.assertEqual(journey.option_id, other.option_id)
            self.assertEqual(journey.resource_refs(), other.resource_refs())

    def test_recovered_escalation_retries_on_supply_restore(self) -> None:
        wheel = self.journey("req-swim-02")
        self.clock.advance_to(at(6, 20))
        self.svc.suspend_trip("metro-3")
        self.clock.advance_to(at(6, 35))
        self.svc.close_security_window("W-AQU-2")
        self.assertEqual("ESCALATED", wheel.node)
        # 全新进程从日志恢复，随后供给恢复
        recovered = MobilityService.restore(
            build_scenario().registry, self.store, Clock(at(6, 36)))
        wheel2 = recovered.journeys["J-req-swim-02"]
        self.assertEqual("ESCALATED", wheel2.node)
        recovered.clock.advance_to(at(6, 40))
        recovered.reopen_security_window("W-AQU-2")
        self.assertEqual("CONFIRMED", wheel2.node)


if __name__ == "__main__":
    unittest.main()
