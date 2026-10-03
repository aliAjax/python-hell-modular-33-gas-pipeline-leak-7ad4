import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


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
                    basis_version INTEGER NOT NULL DEFAULT 1,
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
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
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
                CREATE TABLE IF NOT EXISTS batch_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, page_number),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                """
            )
            # 迁移：为旧库补充 basis_version 列
            columns = [row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
            if "basis_version" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN basis_version INTEGER NOT NULL DEFAULT 1")
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _row_to_source(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _row_to_batch_page(self, row):
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

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,basis_version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                # 重复上报只返回原记录，不再抛冲突
                existing = conn.execute(
                    "SELECT * FROM items WHERE entity_type=? AND stable_key=?",
                    (entity_type, stable_key),
                ).fetchone()
                conn.execute("ROLLBACK")
                return self._row_to_item(existing)
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
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

    def _upsert_sources(self, conn, item_id, sources, actor, role, reason):
        """在事务内 upsert 来源并重新评定。返回 (results, changed, voided, basis, new_basis_version)。"""
        item = self._row_to_item(
            conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        )
        if item is None:
            raise NotFoundError("item_not_found", "业务实体不存在")
        existing_rows = conn.execute(
            "SELECT * FROM sources WHERE item_id=?", (item_id,)
        ).fetchall()
        existing = {(r["source_type"], r["external_id"]): r for r in existing_rows}
        changed = False
        results = []
        for source in sources:
            key = (source["source_type"], source["external_id"])
            row = existing.get(key)
            if row is None:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source["source_type"], source["external_id"],
                     canonical_json(source["payload"]), source["observed_at"], now_iso()),
                )
                source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self.append_audit(conn, item_id, "source_recorded", actor, role, {
                    "source_id": source_id,
                    "source_type": source["source_type"],
                    "external_id": source["external_id"],
                })
                changed = True
                results.append({
                    "id": source_id,
                    "source_type": source["source_type"],
                    "external_id": source["external_id"],
                    "status": "recorded",
                })
            else:
                old_payload = json.loads(row["payload"])
                if old_payload == source["payload"] and row["observed_at"] == source["observed_at"]:
                    results.append({
                        "id": row["id"],
                        "source_type": source["source_type"],
                        "external_id": source["external_id"],
                        "status": "duplicate",
                    })
                else:
                    conn.execute(
                        "UPDATE sources SET payload=?,observed_at=? WHERE id=?",
                        (canonical_json(source["payload"]), source["observed_at"], row["id"]),
                    )
                    self.append_audit(conn, item_id, "source_replaced", actor, role, {
                        "source_id": row["id"],
                        "source_type": source["source_type"],
                        "external_id": source["external_id"],
                        "old": old_payload,
                        "new": source["payload"],
                    })
                    changed = True
                    results.append({
                        "id": row["id"],
                        "source_type": source["source_type"],
                        "external_id": source["external_id"],
                        "status": "replaced",
                    })
        voided = []
        basis = None
        new_basis_version = item["basis_version"]
        if changed:
            all_sources = [self._row_to_source(r) for r in conn.execute(
                "SELECT * FROM sources WHERE item_id=?", (item_id,)
            ).fetchall()]
            basis = rules.compute_basis(item, all_sources)
            new_basis_version = item["basis_version"] + 1
            new_payload = dict(item["payload"])
            voided = rules.void_conclusions(new_payload)
            new_status = "reported" if item["status"] != "cancelled" else item["status"]
            conn.execute(
                "UPDATE items SET status=?,version=version+1,basis_version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, new_basis_version, canonical_json(new_payload), now_iso(), item_id),
            )
            self.append_audit(conn, item_id, "basis_changed", actor, role, {
                "reason": reason,
                "old_basis_version": item["basis_version"],
                "new_basis_version": new_basis_version,
                "basis": basis,
            })
            if voided:
                self.append_audit(conn, item_id, "conclusions_voided", actor, role, {
                    "voided": voided,
                    "reason": reason,
                })
        return results, changed, voided, basis, new_basis_version

    def upsert_sources_and_reassess(self, item_id, sources, actor, role, reason="source_changed"):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            results, changed, voided, basis, new_basis_version = self._upsert_sources(
                conn, item_id, sources, actor, role, reason
            )
            conn.execute("COMMIT")
            return {
                "item": self.get_item(item_id),
                "results": results,
                "changed": changed,
                "voided": voided,
                "basis": basis,
                "basis_version": new_basis_version,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
            return [self._row_to_source(row) for row in rows]
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload,
                     event_payload, expected_version=None, basis_version=None, submitted_basis=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = self._row_to_item(row)
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            if basis_version is not None:
                if int(basis_version) != int(item["basis_version"]):
                    current_basis = rules.compute_basis(
                        item,
                        [self._row_to_source(r) for r in conn.execute(
                            "SELECT * FROM sources WHERE item_id=?", (item_id,)
                        ).fetchall()],
                    )
                    diff = rules.basis_diff(current_basis, submitted_basis)
                    raise ConflictError("basis_version_conflict", "依据版本已更新，请重新读取后再操作", {
                        "current_basis_version": item["basis_version"],
                        "submitted_basis_version": basis_version,
                        "current_basis": current_basis,
                        "submitted_basis": submitted_basis,
                        "diff": diff,
                    })
                if submitted_basis is not None:
                    current_basis = rules.compute_basis(
                        item,
                        [self._row_to_source(r) for r in conn.execute(
                            "SELECT * FROM sources WHERE item_id=?", (item_id,)
                        ).fetchall()],
                    )
                    diff = rules.basis_diff(current_basis, submitted_basis)
                    if diff:
                        raise ConflictError("basis_mismatch", "提交的依据与现场依据不一致", {
                            "current_basis_version": item["basis_version"],
                            "current_basis": current_basis,
                            "submitted_basis": submitted_basis,
                            "diff": diff,
                        })
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

    def get_batch_page(self, batch_id, page_number):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM batch_pages WHERE batch_id=? AND page_number=?",
                (batch_id, page_number),
            ).fetchone()
            return self._row_to_batch_page(row)
        finally:
            conn.close()

    def store_batch_page(self, batch_id, page_number, item_id, sources, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM batch_pages WHERE batch_id=? AND page_number=?",
                (batch_id, page_number),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                return {"page": self._row_to_batch_page(existing), "duplicate": True,
                        "changed": False, "voided": [], "basis": None, "basis_version": None}
            conn.execute(
                "INSERT INTO batch_pages(batch_id,page_number,item_id,payload,source_count,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, page_number, item_id, canonical_json(sources), len(sources), now_iso()),
            )
            page_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            results, changed, voided, basis, new_basis_version = self._upsert_sources(
                conn, item_id, sources, actor, role, "batch_page:%s" % page_number
            )
            self.append_audit(conn, item_id, "batch_page_stored", actor, role, {
                "batch_id": batch_id,
                "page_number": page_number,
                "page_id": page_id,
                "source_count": len(sources),
                "changed": changed,
            })
            conn.execute("COMMIT")
            page = self.get_batch_page(batch_id, page_number)
            return {"page": page, "duplicate": False, "changed": changed,
                    "voided": voided, "basis": basis, "basis_version": new_basis_version, "results": results}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_batch_pages(self, batch_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM batch_pages WHERE batch_id=? ORDER BY page_number",
                (batch_id,),
            ).fetchall()
            return [self._row_to_batch_page(row) for row in rows]
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
