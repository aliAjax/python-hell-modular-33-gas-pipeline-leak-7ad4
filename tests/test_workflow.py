import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def create(self, **overrides):
        payload = {
            "pipeline_id": "P-1",
            "segment_id": "S-8",
            "reported_at": "2026-09-27T08:00:00+00:00",
            "pressure_drop_kpa": 30,
            "sensor_value_ppm": 120,
            "odor_reports": 3,
            "reporter": "dispatch-1",
        }
        payload.update(overrides)
        item, _ = self.service.create_item(payload, "dispatch-1", "dispatcher")
        return item

    def test_complete_leak_workflow(self):
        item = self.create()
        self.assertEqual(item["payload"]["basis_revision"], 1)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")
        self.assertEqual(item["payload"]["assessment"]["basis_revision"], 1)
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-1", "supervisor",
                                item["version"], expected_basis_revision=item["payload"]["basis_revision"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "tech-1", "technician",
                                item["version"], expected_basis_revision=item["payload"]["basis_revision"])
        item = self.service.act(item["id"], "pressure_test",
                                {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100},
                                "tech-1", "technician", item["version"],
                                expected_basis_revision=item["payload"]["basis_revision"])
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "sup-1", "supervisor",
                                item["version"], expected_basis_revision=item["payload"]["basis_revision"])
        self.assertEqual(item["status"], "restored")
        self.assertGreaterEqual(len(item["audit"]), 6)
        self.assertTrue(item["audit_chain_valid"])

    def test_duplicate_item_returns_original(self):
        first = self.create()
        payload = {
            "pipeline_id": "P-1", "segment_id": "S-8",
            "reported_at": "2026-09-27T08:00:00+00:00", "pressure_drop_kpa": 30,
            "sensor_value_ppm": 120, "odor_reports": 3, "reporter": "dispatch-1",
        }
        second, created = self.service.create_item(payload, "dispatch-1", "dispatcher")
        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])

    def test_duplicate_source_returns_original(self):
        item = self.create()
        source_payload = {"source_type": "patrol", "external_id": "PT-7",
                          "observed_at": "2026-09-27T08:05:00+00:00", "sensor_value_ppm": 200}
        source1, _, meta1 = self.service.add_source(item["id"], source_payload, "patrol-1", "patrol")
        source2, item, meta2 = self.service.add_source(item["id"], source_payload, "patrol-1", "patrol")
        self.assertEqual(meta1["outcome"], "added")
        self.assertEqual(meta2["outcome"], "duplicate")
        self.assertEqual(source1["id"], source2["id"])
        # 重复上报不升依据版本、不新增审计
        self.assertEqual(item["payload"]["basis_revision"], 2)
        audit_kinds = [event["event_type"] for event in item["audit"]]
        self.assertEqual(audit_kinds.count("source_recorded"), 1)

    def test_source_added_before_verify_reassesses(self):
        item = self.create(pressure_drop_kpa=1, sensor_value_ppm=2, odor_reports=0)
        self.assertEqual(self.service.get_item(item["id"])["basis_current"]["level"], "low")
        _, item, _ = self.service.add_source(item["id"], {
            "source_type": "sensor", "external_id": "SN-1",
            "observed_at": "2026-09-27T08:06:00+00:00", "sensor_value_ppm": 400, "pressure_drop_kpa": 60,
        }, "sensor-gw", "sensor")
        basis = self.service.get_item(item["id"])["basis_current"]
        self.assertEqual(basis["level"], "critical")
        self.assertEqual(item["payload"]["basis_revision"], 2)
        self.assertIn("SN-1", [e["external_id"] for e in basis["evidence"]])
        # 未核验过，状态仍是待核验，不产生作废
        self.assertEqual(item["status"], "reported")

    def test_source_update_after_verify_voids_all_and_returns_to_pending(self):
        item = self.create()
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor",
                                item["version"], expected_basis_revision=1)
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "t", "technician",
                                item["version"], expected_basis_revision=1)
        item = self.service.act(item["id"], "pressure_test",
                                {"test_passed": True, "pressure_kpa": 150}, "t", "technician",
                                item["version"], expected_basis_revision=1)
        # 晚到补报：来源新增触发依据重评，旧结论全部作废
        _, item, meta = self.service.add_source(item["id"], {
            "source_type": "patrol", "external_id": "PT-9",
            "observed_at": "2026-09-27T10:00:00+00:00", "sensor_value_ppm": 5, "pressure_drop_kpa": 0,
        }, "patrol-2", "patrol")
        self.assertTrue(meta["basis_changed"])
        self.assertEqual(item["status"], "reported")
        self.assertNotIn("assessment", item["payload"])
        self.assertNotIn("valve_sequence", item["payload"])
        self.assertNotIn("repair", item["payload"])
        self.assertNotIn("pressure_test", item["payload"])
        voided = [event for event in item["audit"] if event["event_type"] == "conclusion_voided"]
        self.assertEqual({v["payload"]["kind"] for v in voided},
                         {"assessment", "verification", "isolation", "repair", "pressure_test"})
        for event in voided:
            self.assertEqual(event["payload"]["new_basis_revision"], 2)
        # 作废结论留痕在 payload 中可查
        self.assertEqual(len(item["payload"]["voided_conclusions"]), 5)
        # 新依据下必须重新核验，旧版本开阀被拒
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                             "s", "supervisor", item["version"], expected_basis_revision=1)
        self.assertEqual(context.exception.code, "basis_conflict")
        diff = context.exception.details["basis_diff"]
        self.assertEqual(diff["expected_revision"], 1)
        self.assertEqual(diff["current_revision"], 2)
        self.assertTrue(diff["evidence_added"])

    def test_source_replacement_reassesses(self):
        item = self.create(pressure_drop_kpa=10, sensor_value_ppm=20, odor_reports=0)
        self.service.add_source(item["id"], {
            "source_type": "patrol", "external_id": "PT-1",
            "observed_at": "2026-09-27T08:10:00+00:00", "sensor_value_ppm": 30,
        }, "p", "patrol")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        self.assertIn(item["payload"]["assessment"]["level"], {"low", "medium"})
        # 同一来源修正数据（替换）
        _, item, meta = self.service.add_source(item["id"], {
            "source_type": "patrol", "external_id": "PT-1",
            "observed_at": "2026-09-27T08:12:00+00:00", "sensor_value_ppm": 480, "pressure_drop_kpa": 50,
        }, "p", "patrol")
        self.assertEqual(meta["outcome"], "replaced")
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["payload"]["basis_revision"], 3)
        replaced = [e for e in item["audit"] if e["event_type"] == "source_replaced"]
        self.assertEqual(replaced[0]["payload"]["revision"], 2)

    def test_disposal_requires_basis_version(self):
        item = self.create()
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                             "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "basis_version_required")

    def test_terminal_rejects_new_sources(self):
        item = self.create()
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "cancel", {"reason": "误报"}, "s", "supervisor", item["version"])
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.add_source(item["id"], {
                "source_type": "patrol", "external_id": "PT-1",
                "observed_at": "2026-09-27T08:10:00+00:00", "sensor_value_ppm": 30,
            }, "p", "patrol")
        self.assertEqual(context.exception.code, "source_locked")


if __name__ == "__main__":
    unittest.main()
