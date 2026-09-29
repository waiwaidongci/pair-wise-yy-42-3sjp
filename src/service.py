from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EQUIPMENT_KINDS,
                    FIELD_OP_LABELS, RECORD_ROLES, TERMINAL_STATES, TITLE,
                    VIEW_ROLES, batch_fingerprint, classify_first_seen,
                    completion_blockers, dispatch_conflict, equipment_blockers,
                    escalation_required, normalize_equipment_kind,
                    normalize_field_op, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)

EQUIPMENT_ENTITY='设备'; FIELD_BATCH_ENTITY='现场批次'
EQUIPMENT_ROLES=set(['field_commander','logistics','incident_commander'])
FIELD_INGEST_ROLES=set(['field_commander','logistics'])
MISMATCH_ROLES=set(['field_commander','incident_commander','logistics'])


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target in TERMINAL_STATES:
            blockers += equipment_blockers(
                self.repository.open_occupations_for_item(item_id),
                [a for a in self.repository.arrivals_for_item(item_id)
                 if a['state'] == 'arrived'])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 设备登记（水泵组/油锯/运兵车） ----
    def register_equipment(self, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        kind = normalize_equipment_kind(payload.get("kind"))
        label = require_text(payload.get("label", EQUIPMENT_KINDS[kind]), "label", 100)
        equipment = self.repository.register_equipment(code, kind, label, actor)
        self.repository.append_audit("equipment_register", EQUIPMENT_ENTITY,
                                     equipment["id"], actor,
                                     {"code": code, "kind": kind, "label": label})
        return equipment

    def list_equipment(self, role: str) -> list:
        self._view(role)
        return self.repository.list_equipment()

    # ---- 离线补录：按现场批次号接入 ----
    def ingest_field_batch(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FIELD_INGEST_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)  # 任务区不存在 → NotFoundError
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("entries必须是非空数组")
        entries = [self._normalize_entry(item_id, row) for row in raw_entries]
        fingerprint = batch_fingerprint(entries)

        # 同号只认首条：完全一致幂等返回；内容不一致留待核对
        first = self.repository.get_field_batch(batch_no)
        if first is not None:
            decision, reason = classify_first_seen(
                first["first_status"], first["fingerprint"], fingerprint)
            if decision == 'duplicate':
                return {"status": "duplicate", "batch_no": batch_no,
                        "message": reason, "applied": [], "skipped": []}
            if decision == 'mismatch':
                self.repository.add_mismatch(batch_no, item_id, fingerprint,
                                             payload, reason)
                self.repository.append_audit("field_batch_mismatch",
                                             FIELD_BATCH_ENTITY, item_id, actor,
                                             {"batch_no": batch_no, "reason": reason})
                raise ConflictError(reason)
            # 首条因冲突整批未落库 → 按新内容重试
        return self._apply_batch(item_id, batch_no, fingerprint, entries, actor)

    def _normalize_entry(self, item_id: int, row: Any) -> Dict[str, Any]:
        if not isinstance(row, dict):
            raise ValueError("entries中的每一项必须是对象")
        equipment_code = require_text(row.get("equipment_code"),
                                      "equipment_code", 100)
        op = normalize_field_op(row.get("op"))
        detail = row.get("detail", {})
        if not isinstance(detail, dict):
            raise ValueError("detail必须是对象")
        to_item_id: Optional[int] = None
        if op == 'handover':
            to_item_id = row.get("to_item_id")
            if not isinstance(to_item_id, int) or isinstance(to_item_id, bool):
                raise ValueError("handover必须提供整数to_item_id")
            target = self.repository.get_item(to_item_id)
            if target["status"] in TERMINAL_STATES:
                raise ConflictError(f"移交目标任务区{to_item_id}已关闭")
        return {"equipment_code": equipment_code, "op": op,
                "to_item_id": to_item_id, "detail": detail,
                "_item_id": item_id}

    def _apply_batch(self, item_id: int, batch_no: str, fingerprint: str,
                     entries: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        # 判定层决定每条记录是否落库；设备被另一未结束任务占用 → 整批不写
        plan: List[Dict[str, Any]] = []
        conflicts: List[str] = []
        for seq, entry in enumerate(entries):
            code, op = entry["equipment_code"], entry["op"]
            equipment = self.repository.find_equipment_by_code(code)
            if equipment is None:
                conflicts.append(f"设备{code}未登记")
                continue
            if not equipment.get("active", 1):
                conflicts.append(f"设备{code}已停用")
                continue
            apply, note = True, None
            if op == 'dispatch':
                active = self.repository.active_occupation(code)
                message = dispatch_conflict(active, item_id)
                if message:
                    conflicts.append(message)
                    continue
                if active is not None and active["item_id"] == item_id:
                    apply, note = False, "设备已在本任务区，派单幂等跳过"
            elif op in ('evacuation', 'handover'):
                active = self.repository.active_occupation(code)
                if active is None or active["item_id"] != item_id:
                    apply, note = False, f"设备未占用本任务区，{FIELD_OP_LABELS[op]}跳过"
            elif op == 'arrival':
                arrivals = {a["equipment_code"]: a for a in
                            self.repository.arrivals_for_item(item_id)}
                if code in arrivals and arrivals[code]["state"] == 'arrived':
                    apply, note = False, "设备已到场，重复到场跳过"
            plan.append({"seq": seq, "equipment_code": code, "op": op,
                         "to_item_id": entry.get("to_item_id"),
                         "detail": entry["detail"], "apply": apply, "note": note})

        if conflicts:
            self.repository.mark_batch_rejected(batch_no, item_id, fingerprint, actor)
            self.repository.append_audit("field_batch_rejected", FIELD_BATCH_ENTITY,
                                         item_id, actor,
                                         {"batch_no": batch_no, "conflicts": conflicts})
            raise ConflictError("整批未写入：" + "；".join(conflicts))

        try:
            record = self.repository.apply_field_batch(
                batch_no, item_id, fingerprint, plan, actor)
        except sqlite3.IntegrityError as exc:
            # 并发下占用台账被其他任务抢先：同样整批不写
            self.repository.mark_batch_rejected(batch_no, item_id, fingerprint, actor)
            self.repository.append_audit("field_batch_rejected", FIELD_BATCH_ENTITY,
                                         item_id, actor,
                                         {"batch_no": batch_no,
                                          "conflicts": ["并发占用冲突，请重试"]})
            raise ConflictError("整批未写入：设备已被另一条未结束任务占用") from exc

        applied = [{"seq": p["seq"], "equipment_code": p["equipment_code"],
                    "op": p["op"], "to_item_id": p.get("to_item_id")}
                   for p in plan if p["apply"]]
        skipped = [{"seq": p["seq"], "equipment_code": p["equipment_code"],
                    "op": p["op"], "message": p["note"]}
                   for p in plan if not p["apply"]]
        self.repository.append_audit("field_batch_applied", FIELD_BATCH_ENTITY,
                                     item_id, actor,
                                     {"batch_no": batch_no,
                                      "applied_count": record["applied_count"],
                                      "skipped_count": len(skipped)})
        return {"status": "applied", "batch_no": batch_no,
                "applied_count": record["applied_count"],
                "applied": applied, "skipped": skipped}

    def field_ledger(self, item_id: int, role: str) -> Dict[str, Any]:
        """任务区设备占用台账，含关闭前未撤离/移交的设备清单。"""
        self._view(role)
        occupations = self.repository.open_occupations_for_item(item_id)
        arrivals_open = [a for a in self.repository.arrivals_for_item(item_id)
                         if a['state'] == 'arrived']
        return {
            "item_id": item_id,
            "open_occupations": occupations,
            "arrivals_open": arrivals_open,
            "blockers": equipment_blockers(occupations, arrivals_open),
            "entries": self.repository.list_field_entries(item_id),
        }

    def list_mismatches(self, role: str) -> list:
        ensure_role(role, MISMATCH_ROLES)
        return self.repository.list_mismatches("pending")

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
