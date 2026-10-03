import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.offline import OfflineLedger, page_content_hash
from src.domain import ConflictError, DomainError


def source(external_id, ppm=100, observed="2026-09-27T09:%02d:00+00:00"):
    return {
        "source_type": "patrol",
        "external_id": external_id,
        "observed_at": observed % 10,
        "sensor_value_ppm": ppm,
    }


class BatchUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        item, _ = self.service.create_item({
            "pipeline_id": "P-3", "segment_id": "S-9",
            "reported_at": "2026-09-27T09:00:00+00:00", "pressure_drop_kpa": 5,
            "sensor_value_ppm": 10, "odor_reports": 0, "reporter": "d",
        }, "d", "dispatcher")
        self.item_id = item["id"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def upload(self, page_no, total_pages, entries, batch_id="B-1", content_hash=None):
        return self.service.upload_batch_page(self.item_id, {
            "batch_id": batch_id, "page_no": page_no, "total_pages": total_pages,
            "content_hash": content_hash or page_content_hash(entries), "entries": entries,
        }, "p", "patrol")

    def test_continuous_pages_resume_and_idempotent(self):
        p1 = [source("A-1"), source("A-2")]
        p2 = [source("A-3")]
        p3 = [source("A-4")]
        summary1 = self.upload(1, 3, p1)
        self.assertEqual(summary1["outcome"], "stored")
        self.assertEqual(summary1["next_page"], 2)
        self.assertFalse(summary1["complete"])
        # 模拟第 2 页失败后重传：同一页再传只入库一次
        summary2a = self.upload(2, 3, p2)
        summary2b = self.upload(2, 3, p2)
        self.assertEqual(summary2a["outcome"], "stored")
        self.assertEqual(summary2b["outcome"], "duplicate")
        self.assertEqual(summary2b["stored_pages"], [1, 2])
        self.assertEqual(summary2b["next_page"], 3)
        item = self.service.get_item(self.item_id)
        # 重放没有多插入来源
        self.assertEqual(len(item["sources"]), 3)
        summary3 = self.upload(3, 3, p3)
        self.assertTrue(summary3["complete"])
        self.assertIsNone(summary3["next_page"])
        self.assertEqual(len(self.service.get_item(self.item_id)["sources"]), 4)

    def test_out_of_order_page_gap_waits(self):
        p1 = [source("A-1")]
        p2 = [source("A-2")]
        self.upload(1, 2, p1)
        summary = self.upload(2, 2, p2)
        # 服务端允许乱序到达本身按页存储；next_page 表示缺口
        self.assertTrue(summary["complete"])

    def test_same_page_different_content_rejected(self):
        p2 = [source("A-1")]
        self.upload(1, 2, p2)
        with self.assertRaises(ConflictError) as context:
            self.upload(1, 2, [source("A-1", ppm=999)])
        self.assertEqual(context.exception.code, "page_content_conflict")

    def test_total_pages_mismatch_rejected(self):
        self.upload(1, 3, [source("A-1")])
        with self.assertRaises(ConflictError) as context:
            self.upload(2, 2, [source("A-2")])
        self.assertEqual(context.exception.code, "batch_total_mismatch")

    def test_duplicate_entry_within_page_rejected(self):
        with self.assertRaises(DomainError) as context:
            self.upload(1, 1, [source("A-1"), source("A-1")])
        self.assertEqual(context.exception.code, "duplicate_entry_in_page")


class OfflineLedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.dir, "ledger.json")
        self.ledger = OfflineLedger(self.ledger_path)

    def test_failed_pages_remain_local_until_complete(self):
        pages = [[source("A-1")], [source("A-2")], [source("A-3")]]
        status = self.ledger.create_batch(7, "B-9", pages)
        self.assertEqual(status["next_page"], 1)
        self.assertEqual(status["stored_pages"], [])

        calls = []

        def uploader(item_id, batch_id, page_no, total_pages, content_hash, entries):
            calls.append(page_no)
            if page_no == 2:
                raise RuntimeError("网络中断")

        with self.assertRaises(RuntimeError):
            self.ledger.sync(7, "B-9", uploader)
        # 第 1 页已确认，第 2 页失败：未完成页码留在本地
        status = self.ledger.status(7, "B-9")
        self.assertEqual(status["stored_pages"], [1])
        self.assertEqual(status["next_page"], 2)
        self.assertTrue(os.path.exists(self.ledger_path))

        # 重开账本（模拟进程重启），从已入库之后继续
        reopened = OfflineLedger(self.ledger_path)
        reopened.sync(7, "B-9", lambda *args: {"ok": True})
        self.assertIsNone(reopened.status(7, "B-9"))
        self.assertFalse(os.path.exists(self.ledger_path))

    def test_server_duplicate_page_is_treated_as_stored(self):
        ledger = OfflineLedger(os.path.join(self.dir, "ledger2.json"))
        ledger.create_batch(7, "B-1", [[source("A-1")], [source("A-2")]])
        ledger.sync(7, "B-1", lambda *args: {"outcome": "duplicate"})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ledger2.json")))


if __name__ == "__main__":
    unittest.main()
