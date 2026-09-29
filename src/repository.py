from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    registered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment_arrivals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    equipment_code TEXT NOT NULL,
                    field_batch TEXT,
                    detail TEXT NOT NULL DEFAULT '{{}}',
                    state TEXT NOT NULL DEFAULT 'arrived'
                        CHECK(state IN ('arrived','released')),
                    released_by_op TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, equipment_code)
                );
                CREATE TABLE IF NOT EXISTS equipment_occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    equipment_code TEXT NOT NULL,
                    field_batch TEXT,
                    state TEXT NOT NULL DEFAULT 'occupied'
                        CHECK(state IN ('occupied','released')),
                    released_by_op TEXT,
                    released_to_item_id INTEGER REFERENCES items(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_occupation_active
                    ON equipment_occupations(equipment_code) WHERE state='occupied';
                CREATE TABLE IF NOT EXISTS field_batches (
                    batch_no TEXT PRIMARY KEY,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    first_status TEXT NOT NULL CHECK(first_status IN ('applied','rejected')),
                    fingerprint TEXT NOT NULL,
                    applied_count INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS field_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    equipment_code TEXT NOT NULL,
                    op TEXT NOT NULL,
                    to_item_id INTEGER REFERENCES items(id),
                    detail TEXT NOT NULL DEFAULT '{{}}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_mismatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','resolved')),
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---- 设备登记与台账 ----
    def register_equipment(self, code: str, kind: str, label: str,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO equipment(code, kind, label, active, registered_by, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (code, kind, label, 1, actor, now),
                )
                equipment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设备编码已存在") from exc
        return self.get_equipment(equipment_id)

    def get_equipment(self, equipment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def find_equipment_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list_equipment(self, active: Optional[bool] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM equipment"
        params: tuple = ()
        if active is not None:
            sql += " WHERE active=?"
            params = (1 if active else 0,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def active_occupation(self, equipment_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT o.*, i.title AS item_title FROM equipment_occupations o
                   LEFT JOIN items i ON i.id=o.item_id
                   WHERE o.equipment_code=? AND o.state='occupied'""",
                (equipment_code,)).fetchone()
        return dict(row) if row else None

    def open_occupations_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM equipment_occupations WHERE item_id=? AND state='occupied' ORDER BY id",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def arrivals_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM equipment_arrivals WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 现场批次：同号只认首条、不一致留待核对 ----
    def get_field_batch(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM field_batches WHERE batch_no=?", (batch_no,)).fetchone()
        return dict(row) if row else None

    def create_field_batch(self, batch_no: str, item_id: int, first_status: str,
                           fingerprint: str, applied_count: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO field_batches(batch_no, item_id, first_status, fingerprint,
                   applied_count, actor, created_at) VALUES(?,?,?,?,?,?,?)""",
                (batch_no, item_id, first_status, fingerprint, applied_count, actor, now),
            )
        result = self.get_field_batch(batch_no)
        assert result is not None
        return result

    def mark_batch_rejected(self, batch_no: str, item_id: int, fingerprint: str,
                            actor: str) -> Dict[str, Any]:
        return self.create_field_batch(batch_no, item_id, 'rejected', fingerprint, 0, actor)

    def apply_field_batch(self, batch_no: str, item_id: int, fingerprint: str,
                          plan: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """按判定层给出的计划整批落库：任一写库失败整体回滚。"""
        now = utc_now()
        applied = [p for p in plan if p["apply"]]
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO field_batches(batch_no, item_id, first_status, fingerprint,
                   applied_count, actor, created_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(batch_no) DO UPDATE SET
                     first_status='applied', fingerprint=excluded.fingerprint,
                     applied_count=excluded.applied_count, actor=excluded.actor,
                     created_at=excluded.created_at""",
                (batch_no, item_id, 'applied', fingerprint, len(applied), actor, now),
            )
            self.conn.executemany(
                """INSERT INTO field_entries(batch_no, item_id, seq, equipment_code, op,
                   to_item_id, detail, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                [(batch_no, item_id, p["seq"], p["equipment_code"], p["op"],
                  p.get("to_item_id"),
                  json.dumps(p.get("detail"), ensure_ascii=False), actor, now)
                 for p in plan],
            )
            for p in plan:
                if not p["apply"]:
                    continue
                code, op = p["equipment_code"], p["op"]
                detail = json.dumps(p.get("detail"), ensure_ascii=False)
                if op == 'arrival':
                    self.conn.execute(
                        """INSERT INTO equipment_arrivals(item_id, equipment_code, field_batch,
                           detail, state, created_by, created_at, updated_at)
                           VALUES(?,?,?,?,'arrived',?,?,?)
                           ON CONFLICT(item_id, equipment_code) DO UPDATE SET
                             field_batch=excluded.field_batch, detail=excluded.detail,
                             state='arrived', released_by_op=NULL, updated_at=excluded.updated_at""",
                        (item_id, code, batch_no, detail, actor, now, now),
                    )
                elif op == 'dispatch':
                    self.conn.execute(
                        """INSERT INTO equipment_occupations(item_id, equipment_code, field_batch,
                           state, created_by, created_at) VALUES(?,?,?,'occupied',?,?)""",
                        (item_id, code, batch_no, actor, now),
                    )
                elif op == 'evacuation':
                    self.conn.execute(
                        """UPDATE equipment_occupations SET state='released', released_by_op='evacuation',
                           released_at=? WHERE equipment_code=? AND state='occupied'""",
                        (now, code),
                    )
                    self.conn.execute(
                        """UPDATE equipment_arrivals SET state='released', released_by_op='evacuation',
                           updated_at=? WHERE item_id=? AND equipment_code=? AND state='arrived'""",
                        (now, item_id, code),
                    )
                elif op == 'handover':
                    target = p["to_item_id"]
                    self.conn.execute(
                        """UPDATE equipment_occupations SET state='released', released_by_op='handover',
                           released_to_item_id=?, released_at=?
                           WHERE equipment_code=? AND state='occupied'""",
                        (target, now, code),
                    )
                    self.conn.execute(
                        """UPDATE equipment_arrivals SET state='released', released_by_op='handover',
                           updated_at=? WHERE item_id=? AND equipment_code=? AND state='arrived'""",
                        (now, item_id, code),
                    )
                    self.conn.execute(
                        """INSERT INTO equipment_occupations(item_id, equipment_code, field_batch,
                           state, created_by, created_at) VALUES(?,?,?,'occupied',?,?)""",
                        (target, code, batch_no, actor, now),
                    )
                    self.conn.execute(
                        """INSERT INTO equipment_arrivals(item_id, equipment_code, field_batch,
                           detail, state, created_by, created_at, updated_at)
                           VALUES(?,?,?,?,'arrived',?,?,?)
                           ON CONFLICT(item_id, equipment_code) DO UPDATE SET
                             field_batch=excluded.field_batch, detail=excluded.detail,
                             state='arrived', released_by_op=NULL, updated_at=excluded.updated_at""",
                        (target, code, batch_no, detail, actor, now, now),
                    )
        result = self.get_field_batch(batch_no)
        assert result is not None
        return result

    def add_mismatch(self, batch_no: str, item_id: int, fingerprint: str,
                     payload: Dict[str, Any], reason: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO batch_mismatches(batch_no, item_id, fingerprint, payload,
                   reason, status, created_at) VALUES(?,?,?,?,?,'pending',?)
                   ON CONFLICT(batch_no) DO UPDATE SET
                     reason=excluded.reason, status='pending', resolved_at=NULL""",
                (batch_no, item_id, fingerprint,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), reason, now),
            )
            row = self.conn.execute(
                "SELECT * FROM batch_mismatches WHERE batch_no=?", (batch_no,)).fetchone()
        return dict(row)

    def list_mismatches(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM batch_mismatches"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def list_field_entries(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM field_entries"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = json.loads(entry["detail"]) if entry["detail"] else {}
            result.append(entry)
        return result
