import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="REQUEST_ACCEPTED", payload={})
        self.assertIn(("payload.traveler_role", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_new_event_payloads_require_their_fields(self) -> None:
        for event_type, missing_field in [
            ("REQUEST_QUARANTINED", "payload.business_ref"),
            ("RESOURCE_RELEASED", "payload.resource_ref"),
            ("JOURNEY_LIFECYCLED", "payload.node"),
            ("SUPPLY_CHANGED", "payload.change_kind"),
            ("COMMITMENT_CLOSED", "payload.outcome"),
        ]:
            event = dict(self.sample, event_type=event_type, payload={})
            codes = [(x.field, x.code) for x in validate_event(event, self.schema)]
            self.assertIn((missing_field, "required"), codes, event_type)


if __name__ == "__main__":
    unittest.main()
