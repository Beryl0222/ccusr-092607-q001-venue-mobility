"""可复现的联调场景：构造主数据与申报，供控制台演示与测试复用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .model import (
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

TZ = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 27, hour, minute, tzinfo=TZ)


@dataclass
class Scenario:
    registry: Registry
    requests: list[TravelRequest]
    duplicate: TravelRequest
    quarrel: TravelRequest
    start_at: datetime = field(default_factory=lambda: at(6, 0))


def build_scenario() -> Scenario:
    registry = Registry()
    # 驻地 A（运动员村）、中转 B、场馆 C（游泳中心）、分赛区 D（体育馆）
    registry.add(Lodging("L-VIL", "A", "运动员村"))
    registry.add(Lodging("L-MED", "B", "媒体酒店"))
    registry.add(Venue("V-AQU", "C", "游泳中心"))
    registry.add(Venue("V-GYM", "D", "体育馆"))
    registry.add(Duty("D-SWIM", "V-AQU", "游泳预赛", at(9, 0), ("W-AQU-1", "W-AQU-2")))
    registry.add(Duty("D-GYM", "V-GYM", "体操训练", at(10, 30), ("W-GYM-1",)))

    # 道路：A-B 10 分钟，B-C 15 分钟，A-D 25 分钟（另一条备用通道 A-B-D）
    registry.add(RoadSegment("road-AB", "A", "B", 10))
    registry.add(RoadSegment("road-BC", "B", "C", 15))
    registry.add(RoadSegment("road-AD", "A", "D", 25))
    registry.add(RoadSegment("road-BD", "B", "D", 20))

    # 公共交通：地铁（便宜，容量大）与一条媒体班车轨交
    registry.add(TransitTrip(
        "metro-3", "地铁3号线", "A", "C", at(7, 0), at(7, 40),
        seats=40, wheelchair_spaces=2, equipment_allowed=True))
    registry.add(TransitTrip(
        "metro-3-late", "地铁3号线", "A", "C", at(8, 5), at(8, 45),
        seats=40, wheelchair_spaces=2, equipment_allowed=True))

    # 安检窗口：W-AQU-1 容量小（6 人），W-AQU-2 为无障碍大窗口
    registry.add(SecurityWindow("W-AQU-1", "V-AQU", at(6, 30), at(9, 0),
                                slots=6, accessible=False))
    registry.add(SecurityWindow("W-AQU-2", "V-AQU", at(6, 30), at(9, 30),
                                slots=12, accessible=True))
    registry.add(SecurityWindow("W-GYM-1", "V-GYM", at(8, 0), at(10, 30),
                                slots=8, accessible=True))

    # 司机与车辆
    registry.add(Driver("dr-zhang", "张师傅", frozenset({"medical"})))
    registry.add(Driver("dr-li", "李师傅", frozenset()))
    registry.add(Vehicle("car-01", "dedicated", "A", "dr-zhang",
                         seats=4, wheelchair_spaces=1, equipment_capacity=2,
                         sharable=False, requires=frozenset({"medical"})))
    registry.add(Vehicle("van-01", "shuttle", "B", "dr-li",
                         seats=10, wheelchair_spaces=1, equipment_capacity=4,
                         sharable=True))
    registry.add(Vehicle("van-02", "shuttle", "A", "dr-li",
                         seats=8, wheelchair_spaces=2, equipment_capacity=6,
                         sharable=True))

    requests = [
        # 两名运动员：地铁最便宜
        TravelRequest(
            "req-swim-01", "BIZ-1001", "notice-0001",
            ("athlete-01", "athlete-02"), TravelerRole.ATHLETE,
            party_size=2, wheelchair_count=0, medical=False, equipment_units=1,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6, 0),
            note="携跳水箱 1 件"),
        # 轮椅运动员：硬需求，地铁有轮椅位时仍可成行
        TravelRequest(
            "req-swim-02", "BIZ-1002", "notice-0002",
            ("athlete-03",), TravelerRole.ATHLETE,
            party_size=1, wheelchair_count=1, medical=False, equipment_units=0,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6, 0)),
        # 医疗随护运动员：只能用具医疗资质的专车
        TravelRequest(
            "req-swim-03", "BIZ-1003", "notice-0003",
            ("athlete-04",), TravelerRole.ATHLETE,
            party_size=1, wheelchair_count=0, medical=True, equipment_units=1,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6, 0)),
        # 媒体组：地铁同样满足则成本优先
        TravelRequest(
            "req-media-01", "BIZ-2001", "notice-0010",
            ("media-01", "media-02"), TravelerRole.MEDIA,
            party_size=2, wheelchair_count=0, medical=False, equipment_units=2,
            origin_lodging_id="L-VIL", duty_id="D-SWIM",
            required_arrival=at(8, 40), declared_at=at(6, 0),
            note="摄像器材 2 件"),
    ]
    duplicate = TravelRequest(
        "req-swim-01-dup", "BIZ-1001", "notice-0001",
        ("athlete-01", "athlete-02"), TravelerRole.ATHLETE,
        party_size=2, wheelchair_count=0, medical=False, equipment_units=1,
        origin_lodging_id="L-VIL", duty_id="D-SWIM",
        required_arrival=at(8, 40), declared_at=at(6, 5))
    quarrel = TravelRequest(
        "req-swim-01-change", "BIZ-1001", "notice-0099",
        ("athlete-01", "athlete-02"), TravelerRole.ATHLETE,
        party_size=2, wheelchair_count=0, medical=False, equipment_units=1,
        origin_lodging_id="L-VIL", duty_id="D-SWIM",
        required_arrival=at(10, 40), declared_at=at(6, 10),
        note="同业务编号但时间改为晚场")
    return Scenario(registry, requests, duplicate, quarrel)
