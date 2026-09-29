from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import STATES


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
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    device_type TEXT NOT NULL CHECK(device_type IN ('pump','chainsaw','troop_carrier')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    ref TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','finished')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','released')),
                    batch_id INTEGER REFERENCES ingest_batches(id),
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_device_occupations_device
                    ON device_occupations(device_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_device_occupations_active
                    ON device_occupations(device_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS device_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occupation_id INTEGER NOT NULL
                        REFERENCES device_occupations(id) ON DELETE CASCADE,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    batch_id INTEGER NOT NULL REFERENCES ingest_batches(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('dispatch','arrival','evacuation','handover')),
                    detail TEXT NOT NULL DEFAULT '',
                    seq INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(occupation_id, kind)
                );
                CREATE TABLE IF NOT EXISTS ingest_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'accepted'
                        CHECK(status IN ('accepted')),
                    entry_count INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    existing_fingerprint TEXT NOT NULL,
                    incoming_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
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

    # ---- 设备与任务（占用台账主数据） ----
    def create_device(self, code: str, name: str, device_type: str,
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO devices(code, name, device_type, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (code, name, device_type, actor, now),
                )
                device_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设备编号已存在") from exc
        return self.get_device(device_id)

    def get_device(self, device_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def get_device_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM devices WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list_devices(self, device_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM devices"
        params: tuple = ()
        if device_type:
            sql += " WHERE device_type=?"
            params = (device_type,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def create_task(self, item_id: int, ref: str, title: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO tasks(item_id, ref, title, status, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, ref, title, "open", actor, now, now),
                )
                task_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务编号已存在") from exc
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return dict(row)

    def get_task_by_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE ref=?", (ref,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def finish_task(self, task_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE tasks SET status='finished', updated_at=? WHERE id=? AND status='open'",
                (now, task_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("任务不存在")
                raise ConflictError("任务已结束")
        return self.get_task(task_id)

    def open_occupations_for_task(self, task_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT d.code AS device_code, d.device_type AS device_type
                   FROM device_occupations o JOIN devices d ON d.id=o.device_id
                   WHERE o.task_id=? AND o.status='active'
                   AND NOT EXISTS(SELECT 1 FROM device_records r
                                  WHERE r.occupation_id=o.id
                                  AND r.kind IN ('evacuation','handover'))""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def open_occupations_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        # 任务区关闭前的台账判定：仍缺撤离/移交记录的设备
        with self._lock:
            rows = self.conn.execute(
                """SELECT DISTINCT d.code AS device_code, d.device_type AS device_type
                   FROM device_occupations o
                   JOIN tasks t ON t.id=o.task_id
                   JOIN devices d ON d.id=o.device_id
                   WHERE t.item_id=? AND o.status='active'
                   AND NOT EXISTS(SELECT 1 FROM device_records r
                                  WHERE r.occupation_id=o.id
                                  AND r.kind IN ('evacuation','handover'))""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def occupation_states(self, device_ids: List[int]) -> List[Dict[str, Any]]:
        if not device_ids:
            return []
        marks = ",".join("?" for _ in device_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT o.id AS occupation_id, o.device_id AS device_id,
                           o.task_id AS task_id, o.status AS status,
                           EXISTS(SELECT 1 FROM device_records r
                                  WHERE r.occupation_id=o.id AND r.kind='arrival') AS arrived,
                           EXISTS(SELECT 1 FROM device_records r
                                  WHERE r.occupation_id=o.id
                                  AND r.kind IN ('evacuation','handover')) AS terminal
                    FROM device_occupations o WHERE o.device_id IN ({marks})
                    ORDER BY o.id""",
                tuple(device_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_occupations(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT o.id AS occupation_id, o.status AS status,
                          o.created_at AS created_at, o.released_at AS released_at,
                          d.code AS device_code, d.name AS device_name,
                          d.device_type AS device_type,
                          t.ref AS task_ref, t.title AS task_title, t.status AS task_status
                   FROM device_occupations o
                   JOIN devices d ON d.id=o.device_id
                   JOIN tasks t ON t.id=o.task_id
                   WHERE t.item_id=? ORDER BY o.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_device_records(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.id, r.kind, r.detail, r.seq, r.created_by, r.created_at,
                          d.code AS device_code, d.device_type AS device_type,
                          t.ref AS task_ref, b.batch_no AS batch_no
                   FROM device_records r
                   JOIN devices d ON d.id=r.device_id
                   JOIN tasks t ON t.id=r.task_id
                   JOIN ingest_batches b ON b.id=r.batch_id
                   WHERE b.item_id=? ORDER BY r.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 离线批次：同号只认首条 + 整批原子写入 ----
    def get_batch(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM ingest_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        return dict(row) if row else None

    def list_batches(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ingest_batches"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def apply_batch(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        # plan 由 service 完成全部判定后给出；本方法只负责一次性落账，任何冲突整批回滚
        item_id = plan["item_id"]
        with self._lock, self.conn:
            now = utc_now()
            cur = self.conn.execute(
                """INSERT INTO ingest_batches(item_id, batch_no, fingerprint, status,
                   entry_count, created_by, created_at) VALUES(?,?,?,'accepted',?,?,?)""",
                (item_id, plan["batch_no"], plan["fingerprint"],
                 len(plan["entries"]), plan["actor"], now),
            )
            batch_id = int(cur.lastrowid)
            task_ids: Dict[str, int] = {}
            for task in plan["new_tasks"]:
                tcur = self.conn.execute(
                    """INSERT INTO tasks(item_id, ref, title, status, created_by,
                       created_at, updated_at) VALUES(?,?,?,'open',?,?,?)""",
                    (item_id, task["ref"], task["title"], plan["actor"], now, now),
                )
                task_ids[task["ref"]] = int(tcur.lastrowid)
            for task in plan["existing_tasks"]:
                task_ids[task["ref"]] = task["id"]
            occupation_keys: Dict[str, int] = {}
            created_occupations = 0
            for action in plan["actions"]:
                task_id = task_ids[action["task_ref"]]
                if action["kind"] == "dispatch":
                    ocur = self.conn.execute(
                        """INSERT INTO device_occupations(device_id, task_id, status,
                           batch_id, created_at) VALUES(?,?,'active',?,?)""",
                        (action["device_id"], task_id, batch_id, now),
                    )
                    occupation_id = int(ocur.lastrowid)
                    self.conn.execute(
                        """INSERT INTO device_records(occupation_id, device_id, task_id,
                           batch_id, kind, detail, seq, created_by, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (occupation_id, action["device_id"], task_id, batch_id,
                         "dispatch", action.get("detail", ""), action["seq"],
                         plan["actor"], now),
                    )
                    occupation_keys[action["occupation_key"]] = occupation_id
                    created_occupations += 1
                else:
                    key = action["occupation_key"]
                    occupation_id = occupation_keys[key] if isinstance(key, str) else key
                    if action["kind"] in ("evacuation", "handover"):
                        self.conn.execute(
                            """UPDATE device_occupations SET status='released',
                               released_at=? WHERE id=? AND status='active'""",
                            (now, occupation_id),
                        )
                    self.conn.execute(
                        """INSERT INTO device_records(occupation_id, device_id, task_id,
                           batch_id, kind, detail, seq, created_by, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (occupation_id, action["device_id"], task_id, batch_id,
                         action["kind"], action.get("detail", ""), action["seq"],
                         plan["actor"], now),
                    )
        return {
            "id": batch_id, "batch_no": plan["batch_no"],
            "fingerprint": plan["fingerprint"], "status": "accepted",
            "entry_count": len(plan["entries"]),
            "tasks_created": len(plan["new_tasks"]),
            "occupations_created": created_occupations,
        }

    def record_review(self, batch_no: str, item_id: int, existing_fingerprint: str,
                      incoming_fingerprint: str, payload: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO batch_reviews(batch_no, item_id, existing_fingerprint,
                   incoming_fingerprint, payload, note, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (batch_no, item_id, existing_fingerprint, incoming_fingerprint, payload,
                 "同批次号内容不一致，留待核对", actor, now),
            )
            review_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute("SELECT * FROM batch_reviews WHERE id=?", (review_id,)).fetchone()
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def list_reviews(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_reviews ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result
