import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from .rules import build_basis, void_conclusions


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS basis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    score REAL,
                    level TEXT,
                    basis TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, revision)
                );
                CREATE TABLE IF NOT EXISTS source_batch_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    page_no INTEGER NOT NULL,
                    total_pages INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(item_id, batch_id, page_no)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_legacy(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_source "
                "ON sources(item_id, source_type, external_id) WHERE active = 1"
            )
        finally:
            conn.close()

    def _migrate_legacy(self, conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if columns and "active" not in columns:
            # 旧表是 UNIQUE(item_id, source_type, external_id)，无法承载替换历史，按新结构重建并回填
            conn.executescript(
                """
                ALTER TABLE sources RENAME TO sources_legacy;
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX ux_active_source
                    ON sources(item_id, source_type, external_id) WHERE active = 1;
                INSERT INTO sources(id,item_id,source_type,external_id,payload,observed_at,revision,active,created_at,updated_at)
                    SELECT id,item_id,source_type,external_id,payload,observed_at,1,1,created_at,created_at FROM sources_legacy;
                DROP TABLE sources_legacy;
                """
            )
        for row in conn.execute("SELECT id, payload FROM items").fetchall():
            item_id = row["id"]
            if conn.execute(
                "SELECT 1 FROM basis_snapshots WHERE item_id=? AND revision=1", (item_id,)
            ).fetchone():
                continue
            payload = json.loads(row["payload"])
            sources = self._active_sources(conn, item_id)
            basis = build_basis(payload, sources)
            self._insert_basis(conn, item_id, 1, basis)
            if "basis_revision" not in payload:
                payload["basis_revision"] = 1
                payload["basis_digest"] = self._digest(basis)
                conn.execute(
                    "UPDATE items SET payload=? WHERE id=?", (canonical_json(payload), item_id)
                )

    # ---- 依据版本 ----

    @staticmethod
    def _digest(basis):
        return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()

    def _insert_basis(self, conn, item_id, revision, basis):
        digest = self._digest(basis)
        conn.execute(
            "INSERT INTO basis_snapshots(item_id,revision,digest,score,level,basis,created_at) VALUES(?,?,?,?,?,?,?)",
            (item_id, revision, digest, basis["score"], basis["level"], canonical_json(basis), now_iso()),
        )
        return digest

    def _get_basis_locked(self, conn, item_id, revision):
        row = conn.execute(
            "SELECT * FROM basis_snapshots WHERE item_id=? AND revision=?", (item_id, revision)
        ).fetchone()
        if row is None:
            raise NotFoundError("basis_not_found", "依据版本不存在")
        return json.loads(row["basis"])

    def _basis_snapshot_row_locked(self, conn, item_id, revision):
        row = conn.execute(
            "SELECT * FROM basis_snapshots WHERE item_id=? AND revision=?", (item_id, revision)
        ).fetchone()
        if row is None:
            raise NotFoundError("basis_not_found", "依据版本不存在")
        result = json.loads(row["basis"])
        result["revision"] = row["revision"]
        result["digest"] = row["digest"]
        return result

    def get_basis(self, item_id, revision=None):
        conn = self.connect()
        try:
            if revision is None:
                row = conn.execute(
                    "SELECT * FROM basis_snapshots WHERE item_id=? ORDER BY revision DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM basis_snapshots WHERE item_id=? AND revision=?",
                    (item_id, revision),
                ).fetchone()
            if row is None:
                raise NotFoundError("basis_not_found", "依据版本不存在")
            result = json.loads(row["basis"])
            result["revision"] = row["revision"]
            result["digest"] = row["digest"]
            return result
        finally:
            conn.close()

    def basis_history(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT revision,digest,score,level,created_at FROM basis_snapshots WHERE item_id=? ORDER BY revision",
                (item_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ---- 审计 ----

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    # ---- 事件 ----

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (entity_type, stable_key, initial_status, 1, canonical_json(payload), actor, role, now_iso(), now_iso()),
                )
            except sqlite3.IntegrityError:
                # 重复上报只返回原记录
                conn.execute("COMMIT")
                row = conn.execute(
                    "SELECT * FROM items WHERE entity_type=? AND stable_key=?", (entity_type, stable_key)
                ).fetchone()
                return self._row_to_item(row), False
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            basis = build_basis(payload, [])
            digest = self._insert_basis(conn, item_id, 1, basis)
            payload["basis_revision"] = 1
            payload["basis_digest"] = digest
            conn.execute("UPDATE items SET payload=? WHERE id=?", (canonical_json(payload), item_id))
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key, "basis_revision": 1})
            conn.execute("COMMIT")
            return self.get_item(item_id), True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    # ---- 来源 ----

    @staticmethod
    def _source_body(payload, observed_at):
        body = {key: value for key, value in payload.items() if key not in ("source_type", "external_id")}
        body["observed_at"] = observed_at
        return body

    def _active_sources(self, conn, item_id):
        rows = conn.execute(
            "SELECT * FROM sources WHERE item_id=? AND active=1 ORDER BY id", (item_id,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def list_sources(self, item_id, include_inactive=False):
        conn = self.connect()
        try:
            sql = "SELECT * FROM sources WHERE item_id=?"
            if not include_inactive:
                sql += " AND active=1"
            rows = conn.execute(sql + " ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def _upsert_source_locked(self, conn, item_id, source_type, external_id, payload, observed_at):
        body = self._source_body(payload, observed_at)
        body_json = canonical_json(body)
        existing = conn.execute(
            "SELECT * FROM sources WHERE item_id=? AND source_type=? AND external_id=? AND active=1",
            (item_id, source_type, external_id),
        ).fetchone()
        if existing is not None:
            if existing["observed_at"] == observed_at and existing["payload"] == body_json:
                value = dict(existing)
                value["payload"] = json.loads(value["payload"])
                return value, "duplicate", None
            revision = int(existing["revision"]) + 1
            conn.execute(
                "UPDATE sources SET payload=?, observed_at=?, revision=?, updated_at=? WHERE id=?",
                (body_json, observed_at, revision, now_iso(), existing["id"]),
            )
            row = conn.execute("SELECT * FROM sources WHERE id=?", (existing["id"],)).fetchone()
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            return value, "replaced", {"previous_revision": existing["revision"], "revision": revision}
        conn.execute(
            "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,revision,active,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
            (item_id, source_type, external_id, body_json, observed_at, now_iso(), now_iso()),
        )
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value, "added", None

    def _rebuild_basis_locked(self, conn, item_row):
        """按全部有效来源重算依据；内容变化则升版本并作废旧结论，返回 (changed, revision, basis, trace)。"""
        item_pk = item_row["id"]
        current = json.loads(item_row["payload"])
        sources = self._active_sources(conn, item_pk)
        basis = build_basis(current, sources)
        digest = self._digest(basis)
        if digest == current.get("basis_digest"):
            return False, current.get("basis_revision", 1), basis, []
        revision = int(current.get("basis_revision", 1)) + 1
        basis["revision"] = revision
        self._insert_basis(conn, item_pk, revision, basis)
        current["basis_revision"] = revision
        current["basis_digest"] = digest
        trace = []
        new_status = item_row["status"]
        if item_row["status"] != "reported":
            # 已产生评定/处置结论：来源更新后立即失效，事件回到待核验
            trace = void_conclusions(item_row["status"], current)
            new_status = "reported"
        conn.execute(
            "UPDATE items SET status=?, version=version+1, payload=?, updated_at=? WHERE id=?",
            (new_status, canonical_json(current), now_iso(), item_pk),
        )
        return True, revision, basis, trace

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if item_row["status"] in ("restored", "cancelled"):
                raise DomainError("source_locked", "事件已终结，不能再补登来源", 409)
            source, outcome, meta = self._upsert_source_locked(
                conn, item_id, source_type, external_id, payload, observed_at
            )
            # 重复上报：只返回原记录，不触发评定、不升版本、不留新痕迹
            if outcome == "duplicate":
                conn.execute("COMMIT")
                return source, self.get_item(item_id), {"outcome": "duplicate"}
            if outcome == "replaced":
                self.append_audit(conn, item_id, "source_replaced", actor, role, {
                    "source_id": source["id"], "source_type": source_type, "external_id": external_id,
                    "revision": meta["revision"], "previous_revision": meta["previous_revision"],
                })
            else:
                self.append_audit(conn, item_id, "source_recorded", actor, role, {
                    "source_id": source["id"], "source_type": source_type, "external_id": external_id,
                })
            changed, revision, basis, trace = self._rebuild_basis_locked(conn, item_row)
            if changed:
                self.append_audit(conn, item_id, "basis_revised", actor, role, {
                    "basis_revision": revision, "score": basis["score"], "level": basis["level"], "trigger": outcome,
                })
                for entry in trace:
                    self.append_audit(conn, item_id, "conclusion_voided", actor, role, {
                        "kind": entry["kind"], "voided_basis_revision": entry.get("basis_revision"),
                        "new_basis_revision": revision, "reason": entry.get("reason", "basis_superseded"),
                        "record": entry.get("record"),
                    })
            conn.execute("COMMIT")
            return source, self.get_item(item_id), {"outcome": outcome, "basis_changed": changed, "voided": trace}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ---- 离线批次：按连续编号上传，失败后从已入库部分续传，同一页只入库一次 ----

    def ingest_batch_page(self, item_id, batch_id, page_no, total_pages, content_hash, entries, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if item_row["status"] in ("restored", "cancelled"):
                raise DomainError("source_locked", "事件已终结，不能再补登来源", 409)
            if not (1 <= page_no <= total_pages):
                raise DomainError("invalid_page", "页码必须从 1 开始且不超过总页数")
            existing = conn.execute(
                "SELECT * FROM source_batch_pages WHERE item_id=? AND batch_id=? AND page_no=?",
                (item_id, batch_id, page_no),
            ).fetchone()
            stored_pages = {
                row["page_no"]: row
                for row in conn.execute(
                    "SELECT * FROM source_batch_pages WHERE item_id=? AND batch_id=?", (item_id, batch_id)
                ).fetchall()
            }
            if existing is not None:
                # 同一页重传：内容一致则幂等返回；内容不一致拒绝，防止页码被复用成不同数据
                if existing["content_hash"] != content_hash:
                    raise ConflictError("page_content_conflict", "同一页码已入库但内容不一致，不能覆盖")
                conn.execute("COMMIT")
                return self._batch_summary(item_id, batch_id, page_no, total_pages, "duplicate", [], False, [])
            if stored_pages and total_pages != next(iter(stored_pages.values()))["total_pages"]:
                raise ConflictError("batch_total_mismatch", "同一批次的总页数必须一致")
            identities = set()
            page_sources = []
            for entry in entries:
                key = (entry["source_type"], entry["external_id"])
                if key in identities:
                    raise DomainError("duplicate_entry_in_page", "同一页内存在重复来源 %s/%s" % key)
                identities.add(key)
                source, outcome, meta = self._upsert_source_locked(
                    conn, item_id, entry["source_type"], entry["external_id"],
                    entry["payload"], entry["observed_at"],
                )
                page_sources.append((source, outcome, meta))
                if outcome == "replaced":
                    self.append_audit(conn, item_id, "source_replaced", actor, role, {
                        "source_id": source["id"], "source_type": source["source_type"],
                        "external_id": source["external_id"], "revision": meta["revision"],
                        "previous_revision": meta["previous_revision"], "batch_id": batch_id, "page_no": page_no,
                    })
                elif outcome == "added":
                    self.append_audit(conn, item_id, "source_recorded", actor, role, {
                        "source_id": source["id"], "source_type": source["source_type"],
                        "external_id": source["external_id"], "batch_id": batch_id, "page_no": page_no,
                    })
            conn.execute(
                "INSERT INTO source_batch_pages(item_id,batch_id,page_no,total_pages,content_hash,stored_at) VALUES(?,?,?,?,?,?)",
                (item_id, batch_id, page_no, total_pages, content_hash, now_iso()),
            )
            self.append_audit(conn, item_id, "batch_page_stored", actor, role, {
                "batch_id": batch_id, "page_no": page_no, "total_pages": total_pages, "entries": len(entries),
            })
            changed, revision, basis, trace = self._rebuild_basis_locked(conn, item_row)
            if changed:
                self.append_audit(conn, item_id, "basis_revised", actor, role, {
                    "basis_revision": revision, "score": basis["score"], "level": basis["level"],
                    "trigger": "batch", "batch_id": batch_id, "page_no": page_no,
                })
                for entry in trace:
                    self.append_audit(conn, item_id, "conclusion_voided", actor, role, {
                        "kind": entry["kind"], "voided_basis_revision": entry.get("basis_revision"),
                        "new_basis_revision": revision, "reason": entry.get("reason", "basis_superseded"),
                        "record": entry.get("record"),
                    })
            conn.execute("COMMIT")
            return self._batch_summary(item_id, batch_id, page_no, total_pages, "stored", page_sources, changed, trace)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _batch_summary(self, item_id, batch_id, page_no, total_pages, outcome, page_sources, basis_changed, trace):
        conn = self.connect()
        try:
            stored = {
                row["page_no"]
                for row in conn.execute(
                    "SELECT page_no FROM source_batch_pages WHERE item_id=? AND batch_id=?", (item_id, batch_id)
                ).fetchall()
            }
        finally:
            conn.close()
        expected_next = 1
        while expected_next in stored:
            expected_next += 1
        complete = stored == set(range(1, total_pages + 1))
        return {
            "batch_id": batch_id,
            "page_no": page_no,
            "total_pages": total_pages,
            "outcome": outcome,
            "stored_pages": sorted(stored),
            "next_page": expected_next if not complete else None,
            "complete": complete,
            "basis_changed": basis_changed,
            "voided": [{"kind": entry["kind"]} for entry in trace],
            "entries": [
                {"source_id": source["id"], "source_type": source["source_type"],
                 "external_id": source["external_id"], "outcome": entry_outcome}
                for source, entry_outcome, _ in page_sources
            ],
        }

    # ---- 处置动作 ----

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload,
                     expected_version=None, expected_basis_revision=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            current = json.loads(row["payload"])
            if expected_basis_revision is not None:
                if int(expected_basis_revision) != int(current.get("basis_revision", 1)):
                    from .rules import basis_diff
                    expected_basis = self._get_basis_locked(conn, item_id, int(expected_basis_revision))
                    current_basis = self._get_basis_locked(conn, item_id, int(current.get("basis_revision", 1)))
                    raise ConflictError(
                        "basis_conflict",
                        "提交依据版本为 %s，当前有效依据版本为 %s，请按新依据重新处置"
                        % (expected_basis_revision, current.get("basis_revision", 1)),
                        {"reason": "basis_revision_stale", "basis_diff": basis_diff(
                            expected_basis, current_basis,
                            int(expected_basis_revision), int(current.get("basis_revision", 1)))},
                    )
                event_payload = dict(event_payload)
                event_payload["expected_basis_revision"] = int(expected_basis_revision)
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def verify_audit_chain(self, item_id):
        """重算哈希链，供审计核验。"""
        events = self.audit_trail(item_id)
        previous = "GENESIS"
        for event in events:
            reconstructed = {
                "item_id": event["item_id"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "role": event["role"],
                "payload": event["payload"],
                "created_at": event["created_at"],
            }
            if audit_hash(previous, reconstructed) != event["event_hash"] or event["previous_hash"] != previous:
                return False
            previous = event["event_hash"]
        return True

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
