"""离线批次本地账本。

现场断网时先把来源按连续页码落在本地账本；恢复网络后逐页上传：
- 已确认入库的页跳过；
- 中断后从第一个未确认页继续重试；
- 未完成页码始终留在本地，整批完成才清除。
"""
import hashlib
import json
import os
import tempfile


def page_content_hash(entries):
    return hashlib.sha256(canonical_entries(entries).encode("utf-8")).hexdigest()


def canonical_entries(entries):
    return json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class OfflineLedger:
    def __init__(self, path):
        self.path = path
        self.pending = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as handle:
                self.pending = json.load(handle)

    def _save(self):
        if not self.pending:
            if os.path.exists(self.path):
                os.unlink(self.path)
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        descriptor, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.pending, handle, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(tmp_path, self.path)

    @staticmethod
    def _key(item_id, batch_id):
        return "%s|%s" % (item_id, batch_id)

    def create_batch(self, item_id, batch_id, pages):
        """按连续编号 1..N 建立本地批次；pages 为每页 entry 列表的列表。"""
        if not pages:
            raise ValueError("批次至少要有一页")
        records = {}
        for index, entries in enumerate(pages, start=1):
            if not isinstance(entries, list) or not entries:
                raise ValueError("第 %d 页缺少来源条目" % index)
            records[str(index)] = {
                "page_no": index,
                "content_hash": page_content_hash(entries),
                "entries": entries,
                "stored": False,
            }
        self.pending[self._key(item_id, batch_id)] = {
            "item_id": item_id,
            "batch_id": batch_id,
            "total_pages": len(pages),
            "pages": records,
        }
        self._save()
        return self.status(item_id, batch_id)

    def status(self, item_id, batch_id):
        batch = self.pending.get(self._key(item_id, batch_id))
        if batch is None:
            return None
        pages = batch["pages"]
        stored = sorted(int(no) for no, page in pages.items() if page["stored"])
        next_page = 1
        while str(next_page) in pages and pages[str(next_page)]["stored"]:
            next_page += 1
        return {
            "item_id": item_id,
            "batch_id": batch_id,
            "total_pages": batch["total_pages"],
            "stored_pages": stored,
            "next_page": None if next_page > batch["total_pages"] else next_page,
            "complete": next_page > batch["total_pages"],
        }

    def pending_batches(self):
        result = []
        for key in sorted(self.pending):
            item_id, batch_id = key.split("|", 1)
            status = self.status(int(item_id), batch_id)
            if status and not status["complete"]:
                result.append(status)
        return result

    def page(self, item_id, batch_id, page_no):
        batch = self.pending.get(self._key(item_id, batch_id))
        if batch is None:
            raise KeyError("批次不存在")
        page = batch["pages"][str(page_no)]
        return {"page_no": page_no, "total_pages": batch["total_pages"],
                "content_hash": page["content_hash"], "entries": page["entries"]}

    def mark_stored(self, item_id, batch_id, page_no):
        batch = self.pending[self._key(item_id, batch_id)]
        batch["pages"][str(page_no)]["stored"] = True
        status = self.status(item_id, batch_id)
        if status["complete"]:
            # 整批上传完成，清除本地账本
            del self.pending[self._key(item_id, batch_id)]
        self._save()
        return status

    def sync(self, item_id, batch_id, uploader):
        """从已入库部分之后逐页重试，返回最终状态。

        uploader(item_id, batch_id, page_no, total_pages, content_hash, entries) 由调用方
        实现真正的 HTTP 提交；返回服务端的批次摘要。
        """
        while True:
            status = self.status(item_id, batch_id)
            if status is None or status["complete"]:
                return status
            page_no = status["next_page"]
            page = self.page(item_id, batch_id, page_no)
            uploader(item_id, batch_id, page_no, page["total_pages"],
                     page["content_hash"], page["entries"])
            # 服务端确认（含同页幂等重放）后才标记，失败则页码继续留在本地
            self.mark_stored(item_id, batch_id, page_no)
