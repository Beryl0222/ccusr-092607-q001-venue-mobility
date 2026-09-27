import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest

from venue_mobility.audit import AuditView
from venue_mobility.events import Clock
from venue_mobility.events import EventStore
from venue_mobility.scenario import at, build_scenario
from venue_mobility.service import MobilityService


class AuditTestBase(unittest.TestCase):
    def build_log(self):
        scenario = build_scenario()
        svc = MobilityService(scenario.registry, EventStore(), Clock(at(6, 0)))
        svc.submit_many(scenario.requests)
        svc.clock.advance_to(at(6, 20))
        suspend_events = svc.suspend_trip("metro-3")
        self.suspend_trigger = next(
            e.event_id for e in suspend_events if e.event_type == "SUPPLY_CHANGED")
        svc.clock.advance_to(at(6, 35))
        window_events = svc.close_security_window("W-AQU-2")
        self.window_trigger = next(
            e.event_id for e in window_events if e.event_type == "SUPPLY_CHANGED")
        svc.clock.advance_to(at(6, 40))
        svc.reopen_security_window("W-AQU-2")
        return svc


class WhyTests(AuditTestBase):
    def setUp(self) -> None:
        self.svc = self.build_log()
        self.view = AuditView(self.svc.store.stream())

    def test_resolve_by_person_request_business_and_journey(self) -> None:
        self.assertEqual("req-swim-01", self.view.resolve_request("athlete-01"))
        self.assertEqual("req-swim-01", self.view.resolve_request("req-swim-01"))
        self.assertEqual("req-swim-01", self.view.resolve_request("BIZ-1001"))
        self.assertEqual("req-swim-01", self.view.resolve_request("J-req-swim-01"))

    def test_why_reports_current_route_and_basis(self) -> None:
        report = self.view.why("athlete-01")
        self.assertTrue(report.found)
        journey = report.journey
        self.assertEqual("shuttle", journey.mode)
        self.assertEqual(1, len(journey.changes))
        self.assertIn(self.suspend_trigger,
                      journey.changes[0]["trigger_event_id"])
        self.assertTrue(any("成本最低" in r for r in journey.reasons))

    def test_why_unknown_subject(self) -> None:
        self.assertFalse(self.view.why("nobody").found)


class LedgerTests(AuditTestBase):
    def setUp(self) -> None:
        self.svc = self.build_log()
        self.view = AuditView(self.svc.store.stream())

    def test_suspended_transit_balance_returns_to_zero(self) -> None:
        balances = self.view.balances()
        self.assertEqual(0, balances["transit:metro-3:seat"]["used"])

    def test_window_balance_after_release_and_reacquire(self) -> None:
        balances = self.view.balances()
        # 窗口关闭后全部释放，重开后重新占用：余额应等于仍在保障的人数
        self.assertGreaterEqual(balances["window:W-AQU-2"]["used"], 2)

    def test_ledger_entries_carry_journey_and_trigger(self) -> None:
        released = [e for e in self.view.ledger
                    if e.delta < 0 and e.trigger_event_id == self.suspend_trigger]
        refs = {e.resource_ref for e in released}
        self.assertIn("transit:metro-3:seat", refs)
        self.assertTrue(all(e.journey_id.startswith("J-") for e in released))


class ImpactTests(AuditTestBase):
    def setUp(self) -> None:
        self.svc = self.build_log()
        self.view = AuditView(self.svc.store.stream())

    def test_impact_lists_replaced_journeys_and_freed_refs(self) -> None:
        report = self.view.impact(self.suspend_trigger)
        replaced = {item.journey_id for item in report.items
                    if item.kind == "replaced"}
        self.assertEqual(
            {"J-req-swim-01", "J-req-swim-02", "J-req-media-01"}, replaced)
        for item in report.items:
            self.assertTrue(item.released_refs)
            self.assertTrue(item.new_option_id)

    def test_impact_distinguishes_escalation(self) -> None:
        report = self.view.impact(self.window_trigger)
        escalated = {item.journey_id for item in report.items
                     if item.kind == "escalated"}
        self.assertIn("J-req-swim-02", escalated)
        self.assertIn("J-req-swim-03", escalated)

    def test_impact_unknown_event(self) -> None:
        report = self.view.impact("evt-999999")
        self.assertIsNone(report.trigger)

    def test_completed_list_uses_state_at_trigger_time(self) -> None:
        # 06:20 的停运发生在任何转运闭环之前
        report = self.view.impact(self.suspend_trigger)
        self.assertEqual([], report.untouched_completed)


if __name__ == "__main__":
    unittest.main()
