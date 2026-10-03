import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "pipeline_id": "P-2",
            "segment_id": "S-4",
            "reported_at": "2026-09-27T09:00:00+00:00",
            "pressure_drop_kpa": 20,
            "sensor_value_ppm": 50,
            "odor_reports": 1,
            "reporter": "dispatch-2",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def create(self):
        item, _ = self.service.create_item(self.payload, "d", "dispatcher")
        return item

    def test_duplicate_item_is_idempotent(self):
        item = self.create()
        again, created = self.service.create_item(self.payload, "d", "dispatcher")
        self.assertFalse(created)
        self.assertEqual(again["id"], item["id"])

    def test_valve_conflict(self):
        item = self.create()
        item["payload"]["valve_status_conflict"] = False
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor",
                             item["version"], expected_basis_revision=1)
        self.assertEqual(context.exception.code, "valve_sequence_required")

    def test_version_conflict_and_permission(self):
        item = self.create()
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "x", "sensor", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor",
                             item["version"] - 1, expected_basis_revision=1)

    def test_stale_basis_conflict_has_diff(self):
        item = self.create()
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        self.service.add_source(item["id"], {
            "source_type": "patrol", "external_id": "PT-1",
            "observed_at": "2026-09-27T09:30:00+00:00", "sensor_value_ppm": 400,
        }, "p", "patrol")
        item = self.service.get_item(item["id"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                             "s", "supervisor", item["version"], expected_basis_revision=1)
        self.assertEqual(context.exception.code, "basis_conflict")
        diff = context.exception.details["basis_diff"]
        self.assertEqual(diff["current_revision"], 2)
        self.assertTrue(diff["changed_fields"])


if __name__ == "__main__":
    unittest.main()
