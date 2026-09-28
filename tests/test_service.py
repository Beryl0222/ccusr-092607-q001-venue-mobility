import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.api import ServiceHub
from venue_mobility.contracts import validate_event
from venue_mobility.model import Scenario
from venue_mobility.service import (
    MobilityService,
    ServiceError,
    STATUS_AWAITING_UPGRADE,
    STATUS_CLOSED,
    STATUS_DISPATCHED,
    STATUS_PLANNED,
    STATUS_REVIEWING,
    STATUS_STRANDED,
)
from venue_mobility.clock import ControlledClock


def build_service() -> MobilityService:
    scenario = Scenario.from_dict(json.loads((ROOT / "data/scenario.json").read_text(encoding="utf-8")))
    return MobilityService(scenario, start_at="2026-09-25T06:00:00+08:00")


def athlete(service: MobilityService, ref="B1", **overrides) -> dict:
    params = {
        "business_ref": ref,
        "person_id": f"P-{ref}",
        "person_name": ref,
        "role": "athlete",
        "residence_id": "R1",
        "venue_id": "V1",
        "session_id": "S1",
        "party_size": 1,
    }
    params.update(overrides)
    return service.submit_request(params)


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_transit_is_primary_when_feasible(self) -> None:
        result = athlete(self.service)
        self.assertEqual(result["action"], "planned")
        self.assertEqual(result["plan"]["primary"]["mode"], "transit")
        self.assertEqual(result["plan"]["primary"]["resource_ref"], "T1")

    def test_medical_requires_graded_accessible_vehicle(self) -> None:
        result = athlete(self.service, ref="M1", medical=True)
        primary = result["plan"]["primary"]
        self.assertEqual(primary["resource_ref"], "CAR-A")
        rejected = {item["resource_ref"]: item["reasons"] for item in result["plan"]["rejections"]}
        self.assertIn("transit_not_accessible_graded", rejected["T1"])
        self.assertIn("vehicle_not_medical_graded", rejected["CAR-B"])

    def test_equipment_forces_vehicle_with_capacity(self) -> None:
        result = athlete(self.service, ref="E1", residence_id="R2", venue_id="V2", session_id="S2", equipment_units=1)
        primary = result["plan"]["primary"]
        self.assertEqual(primary["mode"], "vehicle")
        self.assertIn(primary["resource_ref"], {"CAR-C"})

    def test_zone_credential_blocks_vehicle(self) -> None:
        result = athlete(self.service, ref="Z1")
        rejected = {item["resource_ref"]: item["reasons"] for item in result["plan"]["rejections"]}
        self.assertIn("zone_credential_missing", rejected["CAR-D"])

    def test_role_prep_margin_feeds_deadline(self) -> None:
        # 工作人员余量为 0，最晚安检与运动员不同，仍可规划。
        result = self.service.submit_request(
            {
                "business_ref": "ST1",
                "person_id": "P-ST",
                "role": "staff",
                "residence_id": "R1",
                "venue_id": "V1",
                "session_id": "S1",
                "party_size": 1,
            }
        )
        self.assertTrue(result["plan"]["feasible"])


class ReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_vehicle_and_slot_and_seats_reserved_together(self) -> None:
        athlete(self.service, ref="M1", medical=True)
        self.service.confirm("M1")
        ledger = self.service.resource_ledger()["holdings"]
        self.assertEqual(ledger["vehicle:CAR-A"]["held_by"], {"M1": 1})
        self.assertEqual(ledger["slot:K1"]["held_by"]["M1"], 1)

    def test_atomic_contention_picks_backup_or_preempts(self) -> None:
        # 工作人员 4 人占 CAR-A/K1；医疗申报合法抢占，工作人员改到 CAR-B。
        self.service.submit_request(
            {
                "business_ref": "ST4",
                "person_id": "P-ST4",
                "role": "staff",
                "residence_id": "R1",
                "venue_id": "V1",
                "session_id": "S1",
                "party_size": 4,
            }
        )
        self.service.confirm("ST4")
        athlete(self.service, ref="M1", medical=True)
        self.service.confirm("M1")
        ledger = self.service.resource_ledger()["holdings"]
        self.assertEqual(ledger["vehicle:CAR-A"]["held_by"], {"M1": 1})
        self.assertEqual(ledger["vehicle:CAR-B"]["held_by"], {"ST4": 1})

    def test_medical_cannot_be_preempted_by_ordinary_priority(self) -> None:
        athlete(self.service, ref="M1", medical=True)
        self.service.confirm("M1")
        athlete(self.service, ref="A2")  # 普通运动员，不能挤掉医疗档
        # 普通公交仍可走；强制选择唯一医疗车时必须失败。
        plan = self.service._plan(self.service.journeys["A2"].request)
        car_a = next(o for o in (plan.primary, *plan.backups) if o.resource_ref == "CAR-A")
        with self.assertRaises(ServiceError):
            self.service.confirm("A2", option_id=car_a.option_id)

    def test_departed_transfer_is_not_preemptable(self) -> None:
        self.service.submit_request(
            {
                "business_ref": "ST1",
                "person_id": "P-ST1",
                "role": "staff",
                "residence_id": "R1",
                "venue_id": "V1",
                "session_id": "S1",
                "party_size": 4,
            }
        )
        plan = self.service._plan(self.service.journeys["ST1"].request)
        car_b = next(o for o in (plan.primary, *plan.backups) if o.resource_ref == "CAR-B")
        self.service.confirm("ST1", option_id=car_b.option_id)  # 固定占用 CAR-B/K1，出发 07:50
        # 时钟推过出发时刻后不可再抢占。
        self.service.clock.advance_to(__import__("venue_mobility.times", fromlist=["parse"]).parse("2026-09-26T08:00:00+08:00"))
        athlete(self.service, ref="A9")
        plan = self.service._plan(self.service.journeys["A9"].request)
        car_b = next(o for o in (plan.primary, *plan.backups) if o.resource_ref == "CAR-B")
        with self.assertRaises(ServiceError):
            self.service.confirm("A9", option_id=car_b.option_id)


class RequestLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_duplicate_notification_does_not_dispatch_twice(self) -> None:
        first = athlete(self.service, ref="B1", notification_key="N1")
        second = athlete(self.service, ref="B1", notification_key="N1")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(self.service.journeys["B1"].status, STATUS_PLANNED)
        self.assertEqual(first["action"], "planned")

    def test_same_business_ref_different_fingerprint_enters_review(self) -> None:
        athlete(self.service, ref="B1", person_id="P1")
        result = athlete(self.service, ref="B1", person_id="P2")
        self.assertEqual(result["action"], "review")
        self.assertEqual(self.service.journeys["B1"].status, STATUS_REVIEWING)
        resolved = self.service.resolve_review("B1", accept=True)
        self.assertEqual(resolved["action"], "planned")

    def test_rejected_review_closes_request(self) -> None:
        athlete(self.service, ref="B1", person_id="P1")
        athlete(self.service, ref="B1", person_id="P2")
        result = self.service.resolve_review("B1", accept=False)
        self.assertEqual(result["action"], "rejected")


class ClockNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_waiting_expires_into_no_show_and_releases(self) -> None:
        athlete(self.service, ref="B1")
        self.service.confirm("B1")
        self.service.begin_waiting("B1", minutes=15)
        result = self.service.advance(16)
        self.assertEqual(result["fired"], [{"business_ref": "B1", "node": "no_show"}])
        self.assertEqual(self.service.journeys["B1"].status, STATUS_CLOSED)
        self.assertNotIn("transit:T1", self.service.resource_ledger()["holdings"])

    def test_upgrade_node_auto_fires_after_grace(self) -> None:
        athlete(self.service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        self.service.suspend_transit("T2", reason="班次停运")
        self.assertEqual(self.service.journeys["B7"].status, STATUS_AWAITING_UPGRADE)
        result = self.service.advance(20)
        self.assertEqual(result["fired"][0]["node"], "upgrade_auto")
        self.assertEqual(self.service.journeys["B7"].status, STATUS_DISPATCHED)
        self.assertEqual(self.service.journeys["B7"].basis_option["resource_ref"], "CAR-C")


class DisruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_only_affected_journeys_replan(self) -> None:
        athlete(self.service, ref="B1")  # R1->V1 公交
        self.service.confirm("B1")
        athlete(self.service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        result = self.service.suspend_transit("T2", reason="停运")
        self.assertEqual(result["affected"], ["B7"])
        self.assertEqual(self.service.journeys["B1"].status, STATUS_DISPATCHED)

    def test_completed_transfer_keeps_its_basis(self) -> None:
        athlete(self.service, ref="B1")
        self.service.confirm("B1")
        self.service.complete_transfer("B1")
        basis = self.service.journeys["B1"].basis_option
        self.service.suspend_transit("T1", reason="停运")
        self.assertEqual(self.service.journeys["B1"].basis_option, basis)
        self.assertEqual(self.service.journeys["B1"].status, STATUS_CLOSED)

    def test_closure_can_strand_and_later_recovery(self) -> None:
        athlete(self.service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        self.service.suspend_transit("T2", reason="停运")  # 升级等待：公交 T2 → 专车 CAR-C
        result = self.service.impose_closure(
            {
                "closure_id": "C1",
                "residence_id": "R2",
                "venue_id": "V2",
                "starts_at": "2026-09-26T08:15:00+08:00",
                "ends_at": "2026-09-26T09:15:00+08:00",
            },
            reason="管制",
        )  # 同时覆盖 CAR-C/K3（08:20 出发）与更晚 K4（08:50 出发）→ 搁浅
        self.assertIn("B7", result["affected"])
        self.assertEqual(self.service.journeys["B7"].status, STATUS_STRANDED)
        recovered = self.service.lift_closure("C1", reason="管制解除")
        self.assertIn("B7", recovered["affected"])
        self.assertEqual(self.service.journeys["B7"].status, STATUS_PLANNED)

    def test_transit_restore_recovers_stranded_journey(self) -> None:
        # R2 方向全部专车都因管制不可用、公交停运时搁浅；任一恢复即可重新规划。
        athlete(self.service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        self.service.impose_closure(
            {
                "closure_id": "C1",
                "residence_id": "R2",
                "venue_id": "V2",
                "starts_at": "2026-09-26T08:00:00+08:00",
                "ends_at": "2026-09-26T09:00:00+08:00",
            },
            reason="管制",
        )  # 公交不受管制，仍可行，不重排
        self.service.suspend_transit("T2", reason="停运")  # 现在公交也没了 → 搁浅
        self.assertEqual(self.service.journeys["B7"].status, STATUS_STRANDED)
        recovered = self.service.restore_transit("T2")
        self.assertIn("B7", recovered["affected"])
        self.assertEqual(self.service.journeys["B7"].status, STATUS_PLANNED)

    def test_reschedule_only_touches_matching_session(self) -> None:
        athlete(self.service, ref="B1")
        self.service.confirm("B1")
        athlete(self.service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        result = self.service.reschedule_session(
            "S2", "2026-09-26T12:00:00+08:00", "2026-09-26T11:00:00+08:00", reason="临时改项"
        )
        self.assertEqual(result["affected"], [])  # 时间推后，既有路径仍可行


class RecoveryTests(unittest.TestCase):
    def test_rebuild_from_event_log_continues_obligations(self) -> None:
        import tempfile

        service = build_service()
        athlete(service, ref="B7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        service.confirm("B7")
        service.suspend_transit("T2", reason="停运")  # 进入升级等待
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "events.log"
            service.save(store)
            revived = MobilityService.from_store(store, service.scenario)
            self.assertEqual(revived.journeys["B7"].status, STATUS_AWAITING_UPGRADE)
            fired = revived.advance(25)["fired"]
            self.assertEqual(fired[0]["business_ref"], "B7")
            self.assertEqual(revived.journeys["B7"].status, STATUS_DISPATCHED)


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        athlete(self.service, ref="B7", person_id="P7", residence_id="R2", venue_id="V2", session_id="S2", role="media")
        self.service.confirm("B7")
        self.service.suspend_transit("T2", reason="班次停运")
        self.service.advance(20)

    def test_explain_reports_why_and_basis(self) -> None:
        explanation = self.service.explain("P7")
        self.assertEqual(explanation["business_ref"], "B7")
        self.assertIn("替代路径", "".join(explanation["why"]))
        self.assertTrue(explanation["timeline"])

    def test_changes_list_releases_and_replacements(self) -> None:
        changes = self.service.changes()
        self.assertEqual(changes[0]["business_ref"], "B7")
        self.assertEqual(changes[0]["reason"], "班次停运")
        ledger = self.service.resource_ledger()["movements"]
        kinds = [m["type"] for m in ledger]
        self.assertIn("RESOURCE_RELEASED", kinds)
        self.assertIn("RESOURCE_RESERVED", kinds)

    def test_pending_obligations_excludes_closed(self) -> None:
        athlete(self.service, ref="B1")
        self.service.confirm("B1")
        self.service.complete_transfer("B1")
        refs = {item["business_ref"] for item in self.service.pending_obligations()}
        self.assertNotIn("B1", refs)
        self.assertIn("B7", refs)


class ContractTests(unittest.TestCase):
    def test_service_events_match_service_schema(self) -> None:
        schema = json.loads((ROOT / "contracts/service.schema.json").read_text(encoding="utf-8"))
        service = build_service()
        athlete(service, ref="B1")
        service.confirm("B1")
        service.begin_waiting("B1", minutes=15)
        service.advance(16)
        for event in service.events:
            self.assertEqual([], validate_event(event, schema), event["event_type"])


class ApiSurfaceTests(unittest.TestCase):
    def test_dispatch_returns_rule_error_without_raising(self) -> None:
        hub = ServiceHub(Scenario.from_dict(json.loads((ROOT / "data/scenario.json").read_text(encoding="utf-8"))))
        ok = hub.dispatch({"command": "submit", "params": {
            "business_ref": "B1", "person_id": "P1", "role": "athlete",
            "residence_id": "R1", "venue_id": "V1", "session_id": "S1",
        }})
        self.assertTrue(ok["ok"])
        bad = hub.dispatch({"command": "confirm", "params": {"business_ref": "UNKNOWN"}})
        self.assertEqual(bad["error"], "service_rule")


if __name__ == "__main__":
    unittest.main()
