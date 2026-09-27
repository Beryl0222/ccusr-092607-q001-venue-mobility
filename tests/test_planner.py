import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from datetime import timedelta

from venue_mobility.events import Clock
from venue_mobility.model import (
    Driver,
    Duty,
    Lodging,
    Registry,
    RoadSegment,
    SecurityWindow,
    TransitTrip,
    TravelRequest,
    TravelerRole,
    Vehicle,
    Venue,
)
from venue_mobility.planner import Planner, ResourceInventory


def t(hour: int, minute: int = 0):
    from datetime import datetime, timezone
    return datetime(2026, 9, 27, hour, minute, tzinfo=timezone(timedelta(hours=8)))


class World:
    def __init__(self) -> None:
        self.reg = Registry()
        self.reg.add(Lodging("L1", "A", "运动员村"))
        self.reg.add(Venue("V1", "C", "游泳中心"))
        self.reg.add(Duty("D1", "V1", "预赛", t(9, 0), ("W1", "W2")))
        self.reg.add(RoadSegment("s1", "A", "B", 10))
        self.reg.add(RoadSegment("s2", "B", "C", 15))
        self.reg.add(RoadSegment("s3", "A", "C", 40))
        self.reg.add(TransitTrip("T1", "地铁", "A", "C", t(7, 0), t(7, 40),
                                 seats=10, wheelchair_spaces=1, equipment_allowed=True))
        self.reg.add(SecurityWindow("W1", "V1", t(6, 30), t(9, 0),
                                    slots=6, accessible=False))
        self.reg.add(SecurityWindow("W2", "V1", t(6, 30), t(9, 30),
                                    slots=6, accessible=True))
        self.reg.add(Driver("d1", "张师傅", frozenset({"medical"})))
        self.reg.add(Driver("d2", "李师傅", frozenset()))
        self.reg.add(Vehicle("CAR", "dedicated", "A", "d1", 4, 1, 2,
                             False, frozenset({"medical"})))
        self.reg.add(Vehicle("VAN", "shuttle", "A", "d2", 8, 1, 3, True))
        self.inv = ResourceInventory()
        self.planner = Planner(self.reg, self.inv)

    def request(self, **kw) -> TravelRequest:
        defaults = dict(
            request_id="R1", business_ref="B1", notice_id="N1",
            person_ids=("p1",), role=TravelerRole.ATHLETE, party_size=1,
            wheelchair_count=0, medical=False, equipment_units=0,
            origin_lodging_id="L1", duty_id="D1",
            required_arrival=t(8, 40), declared_at=t(6, 0),
        )
        defaults.update(kw)
        return TravelRequest(**defaults)


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()

    def test_transit_is_chosen_when_feasible(self) -> None:
        result = self.world.planner.plan(self.world.request(), now=t(6))
        self.assertEqual("transit", result.chosen.mode)
        self.assertEqual("transit-T1", result.chosen.option_id)
        self.assertLess(result.chosen.cost, 200)

    def test_medical_requires_qualified_driver(self) -> None:
        result = self.world.planner.plan(self.world.request(medical=True), now=t(6))
        self.assertEqual("dedicated", result.chosen.mode)
        self.assertEqual("dedicated-CAR", result.chosen.option_id)
        transit = result.option("transit-T1")
        self.assertTrue(any("医疗" in r for r in transit.rejections))

    def test_medical_cannot_be_covered_by_ordinary_priority(self) -> None:
        # 把唯一具备医疗资质的专车下线，公共交通与无医疗班车都必须不可行
        self.world.reg.set_vehicle_offline("CAR", True)
        result = self.world.planner.plan(self.world.request(medical=True), now=t(6))
        self.assertIsNone(result.chosen)
        self.assertTrue(any("医疗" in r for r in result.escalation_reasons))

    def test_wheelchair_needs_accessible_security_window(self) -> None:
        self.world.reg.set_window_closed("W2", True)
        result = self.world.planner.plan(
            self.world.request(wheelchair_count=1), now=t(6))
        self.assertIsNone(result.chosen)
        self.assertTrue(any("无障碍" in r for r in result.escalation_reasons))

    def test_equipment_counts_against_capacity(self) -> None:
        # 地铁 10 座，4 人 + 8 件器材（公交上器材占座位名额）
        req = self.world.request(person_ids=tuple(f"p{i}" for i in range(4)),
                                 party_size=4, equipment_units=8)
        result = self.world.planner.plan(req, now=t(6))
        transit = result.option("transit-T1")
        self.assertTrue(any("座位" in r or "余票" in r for r in transit.rejections))

    def test_suspended_trip_is_not_offered(self) -> None:
        self.world.reg.set_trip_suspended("T1", True)
        result = self.world.planner.plan(self.world.request(), now=t(6))
        self.assertNotEqual("transit", result.chosen.mode)
        transit = result.option("transit-T1")
        self.assertTrue(any("停运" in r for r in transit.rejections))

    def test_road_restriction_reroutes_or_blocks(self) -> None:
        from venue_mobility.planner import shortest_path
        self.assertEqual(25, shortest_path(self.world.reg, "A", "C"))
        self.world.reg.set_segment_restricted("s1", True)
        self.assertEqual(40, shortest_path(self.world.reg, "A", "C"))
        self.world.reg.set_segment_restricted("s3", True)
        self.assertIsNone(shortest_path(self.world.reg, "A", "C"))

    def test_dedicated_vehicle_is_exclusive(self) -> None:
        r1 = self.world.request(request_id="R1", medical=True)
        r2 = self.world.request(request_id="R2", notice_id="N2",
                                business_ref="B2", person_ids=("p2",), medical=True)
        first = self.world.planner.plan(r1, now=t(6))
        self.assertEqual("dedicated-CAR", first.chosen.option_id)
        self.world.inv.acquire([res.as_hold("R1")
                                for res in first.chosen.reservations])
        second = self.world.planner.plan(r2, now=t(6))
        # 第二人再选到同一专车时，该候选必须已记录整包占用否决
        car = second.option("dedicated-CAR")
        self.assertTrue(any("整包占用" in r for r in car.rejections))


class InventoryTests(unittest.TestCase):
    def test_overlapping_capacity_is_enforced_atomically(self) -> None:
        from datetime import datetime, timezone
        from venue_mobility.planner import Hold
        start = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        inv = ResourceInventory()
        ok = inv.acquire([Hold("v", "seat", start, end, 2, "r1", 2)])
        self.assertTrue(ok)
        # 同批内超出容量 -> 整批失败、不部分生效
        ok = inv.acquire([
            Hold("v", "seat", start, end, 1, "r2", 2),
            Hold("v", "seat", start, end, 1, "r3", 2),
        ])
        self.assertFalse(ok)
        self.assertEqual([h.request_id for h in inv.holds()], ["r1"])

    def test_release_returns_capacity(self) -> None:
        from datetime import datetime, timezone
        from venue_mobility.planner import Hold
        start = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        inv = ResourceInventory()
        inv.acquire([Hold("v", "seat", start, end, 2, "r1", 2)])
        inv.release("r1")
        self.assertEqual(0, inv.used("v", start, end))


if __name__ == "__main__":
    unittest.main()
