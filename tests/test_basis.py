import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError
from src.audit import audit_hash


class BasisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "pipeline_id": "P-9",
            "segment_id": "S-1",
            "reported_at": "2026-10-01T08:00:00+00:00",
            "pressure_drop_kpa": 5,
            "sensor_value_ppm": 10,
            "odor_reports": 0,
            "reporter": "dispatch-1",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _source(self, external_id, ppm, observed_at, source_type="sensor", pressure=None, odor=None):
        payload = {
            "source_type": source_type,
            "external_id": external_id,
            "observed_at": observed_at,
            "sensor_value_ppm": ppm,
        }
        if pressure is not None:
            payload["pressure_drop_kpa"] = pressure
        if odor is not None:
            payload["odor_reports"] = odor
        return payload

    def test_source_add_triggers_reassessment_and_voids(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["basis_version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["basis_version"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "t", "technician", item["basis_version"])
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150}, "t", "technician", item["basis_version"])
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "s", "supervisor", item["basis_version"])
        self.assertEqual(item["status"], "restored")

        # 晚到的高浓度来源
        result = self.service.add_source(item["id"], self._source("S-1", 600, "2026-10-01T09:00:00+00:00", pressure=20), "sensor-1", "sensor")
        self.assertTrue(result["changed"])
        item = self.service.get_item(item["id"])
        # 事件回到待核验
        self.assertEqual(item["status"], "reported")
        # 依据版本递增
        self.assertEqual(item["basis_version"], 2)
        # 评定重新计算
        self.assertEqual(item["assessment"]["level"], "critical")
        # 隔离、抢修、试压、恢复结论一并作废
        self.assertNotIn("valve_sequence", item["payload"])
        self.assertNotIn("repair", item["payload"])
        self.assertNotIn("pressure_test", item["payload"])
        self.assertNotIn("restoration", item["payload"])
        self.assertIn("verification", result["voided"])
        self.assertIn("valve_sequence", result["voided"])
        self.assertIn("repair", result["voided"])
        self.assertIn("pressure_test", result["voided"])
        self.assertIn("restoration", result["voided"])
        # 留痕
        event_types = [e["event_type"] for e in item["audit"]]
        self.assertIn("basis_changed", event_types)
        self.assertIn("conclusions_voided", event_types)

    def test_source_replace_triggers_reassessment(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        self.service.add_source(item["id"], self._source("S-1", 10, "2026-10-01T08:30:00+00:00"), "sensor-1", "sensor")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["basis_version"], 2)
        self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["basis_version"])

        # 替换同一来源（同 source_type + external_id，不同内容）
        result = self.service.add_source(item["id"], self._source("S-1", 500, "2026-10-01T09:30:00+00:00", pressure=20), "sensor-1", "sensor")
        self.assertTrue(result["changed"])
        self.assertEqual(result["results"][0]["status"], "replaced")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["basis_version"], 3)
        self.assertEqual(item["assessment"]["level"], "critical")

    def test_duplicate_source_returns_original(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        source = self._source("S-1", 10, "2026-10-01T08:30:00+00:00")
        first = self.service.add_source(item["id"], source, "sensor-1", "sensor")
        second = self.service.add_source(item["id"], source, "sensor-1", "sensor")
        # 重复上报只返回原记录
        self.assertEqual(first["results"][0]["id"], second["results"][0]["id"])
        self.assertEqual(second["results"][0]["status"], "duplicate")
        self.assertFalse(second["changed"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["basis_version"], 2)

    def test_basis_version_conflict_with_diff(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        self.service.add_source(item["id"], self._source("S-1", 600, "2026-10-01T09:00:00+00:00", pressure=20), "sensor-1", "sensor")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["basis_version"], 2)
        # 用落后的 basis_version 操作
        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", 1)
        self.assertEqual(context.exception.code, "basis_version_conflict")
        details = context.exception.details
        self.assertEqual(details["current_basis_version"], 2)
        self.assertEqual(details["submitted_basis_version"], 1)
        self.assertIn("sensor_value_ppm", details["diff"])
        self.assertEqual(details["diff"]["sensor_value_ppm"]["current"], 600)

    def test_basis_mismatch_conflict(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["basis_version"])
        # basis_version 对得上，但提交的依据快照对不上
        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "isolate", {
                "valve_sequence": ["V-1", "V-2"],
                "basis": {"sensor_value_ppm": 999, "pressure_drop_kpa": 999, "odor_reports": 999},
            }, "s", "supervisor", item["basis_version"])
        self.assertEqual(context.exception.code, "basis_mismatch")
        details = context.exception.details
        self.assertIn("sensor_value_ppm", details["diff"])

    def test_batch_page_idempotent_retry(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        page1_sources = [self._source("S-1", 100, "2026-10-01T08:30:00+00:00")]
        page2_sources = [self._source("S-2", 200, "2026-10-01T08:35:00+00:00")]

        first = self.service.upload_batch_page("B-1", 1, item["id"], page1_sources, "sensor-1", "sensor")
        self.assertFalse(first["duplicate"])
        # 同一页重传只入库一次
        retry = self.service.upload_batch_page("B-1", 1, item["id"], page1_sources, "sensor-1", "sensor")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(first["page"]["id"], retry["page"]["id"])

        self.service.upload_batch_page("B-1", 2, item["id"], page2_sources, "sensor-1", "sensor")
        status = self.service.batch_status("B-1")
        self.assertEqual(status["stored_pages"], [1, 2])
        self.assertEqual(status["next_page"], 3)

        # 来源只入库一次
        item = self.service.get_item(item["id"])
        external_ids = [s["external_id"] for s in item["sources"]]
        self.assertEqual(external_ids.count("S-1"), 1)
        self.assertEqual(external_ids.count("S-2"), 1)

    def test_audit_chain_intact_after_source_change(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["basis_version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["basis_version"])
        self.service.add_source(item["id"], self._source("S-1", 600, "2026-10-01T09:00:00+00:00", pressure=20), "sensor-1", "sensor")
        item = self.service.get_item(item["id"])
        # 校验审计链完整可追溯
        previous = "GENESIS"
        for event in item["audit"]:
            self.assertEqual(event["previous_hash"], previous)
            expected = audit_hash(previous, {
                "item_id": event["item_id"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "role": event["role"],
                "payload": event["payload"],
                "created_at": event["created_at"],
            })
            self.assertEqual(event["event_hash"], expected)
            previous = event["event_hash"]


if __name__ == "__main__":
    unittest.main()
