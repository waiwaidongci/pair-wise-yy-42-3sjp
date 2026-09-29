from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                    normalize_severity, optional_text, require_choice,
                    require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEVICE, DEVICE_ROLES,
                    DEVICE_TYPES, ENTITY, ENTRY_ARRIVAL, ENTRY_DISPATCH,
                    ENTRY_KINDS, INGEST, INGEST_ROLES, RECORD_ROLES,
                    REVIEW_ROLES, TASK, TASK_CREATE_ROLES, TASK_FINISH_ROLES,
                    VIEW_ROLES, batch_fingerprint,
                    device_label, dispatch_conflict_message,
                    entry_label, equipment_close_blockers,
                    completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    task_finish_blockers, validate_transition)


def _normalize_device_entry(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("entries每项必须是对象")
    kind = require_choice(raw.get("kind"), "entry.kind", ENTRY_KINDS)
    entry: Dict[str, Any] = {
        "kind": kind,
        "device_code": require_text(raw.get("device_code"), "device_code", 60),
        "task_ref": require_text(raw.get("task_ref"), "task_ref", 60),
    }
    if kind == "dispatch":
        title = raw.get("task_title")
        if title is not None:
            entry["task_title"] = require_text(title, "task_title", 200)
    else:
        receiver = raw.get("receiver")
        if receiver is not None:
            entry["receiver"] = require_text(receiver, "receiver", 100)
    detail = raw.get("detail")
    if detail is not None:
        entry["detail"] = optional_text(detail, "detail", 2000)
    return entry


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
        if target == "closed":
            blockers = blockers + equipment_close_blockers(
                self.repository.open_occupations_for_item(item_id))
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

    # ---- 设备台账（占用台账主数据） ----
    def register_device(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 60)
        name = require_text(payload.get("name"), "name", 120)
        device_type = require_choice(payload.get("device_type"), "device_type", DEVICE_TYPES)
        device = self.repository.create_device(code, name, device_type, actor)
        self.repository.append_audit("device_register", DEVICE, device["id"], actor, {
            "code": code, "device_type": device_type,
            "device_type_label": device_label(device_type),
        })
        return device

    def list_devices(self, role: str, device_type: Optional[str] = None) -> list:
        self._view(role)
        if device_type is not None:
            require_choice(device_type, "device_type", DEVICE_TYPES)
        return self.repository.list_devices(device_type)

    def create_task(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        ref = require_text(payload.get("ref"), "ref", 60)
        title = require_text(payload.get("title"), "title", 200)
        task = self.repository.create_task(item_id, ref, title, actor)
        self.repository.append_audit("task_create", TASK, task["id"], actor, {
            "item_id": item_id, "ref": ref, "title": title,
        })
        return task

    def finish_task(self, item_id: int, task_ref: str, actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_FINISH_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self._load_task(item_id, task_ref)
        blockers = task_finish_blockers(self.repository.open_occupations_for_task(task["id"]))
        if blockers:
            raise ConflictError("仍有设备未撤离或移交：" + "；".join(blockers))
        updated = self.repository.finish_task(task["id"])
        self.repository.append_audit("task_finish", TASK, task["id"], actor, {
            "item_id": item_id, "ref": task_ref,
        })
        return updated

    def list_tasks(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_tasks(item_id)

    def list_occupations(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_occupations(item_id)

    def list_device_records(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_device_records(item_id)

    def list_reviews(self, role: str) -> list:
        ensure_role(role, REVIEW_ROLES)
        return self.repository.list_reviews()

    def list_batches(self, role: str, item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_batches(item_id)

    # ---- 离线批量补录：同号只认首条，占用冲突整批不写 ----
    def ingest_batch(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, INGEST_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 80)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValidationError("entries必须是非空数组")
        if len(raw_entries) > 500:
            raise ValidationError("单批补录不能超过500条")
        entries = [_normalize_device_entry(e) for e in raw_entries]
        fingerprint = batch_fingerprint(entries)

        existing = self.repository.get_batch(batch_no)
        if existing is not None:
            if existing["fingerprint"] == fingerprint:
                # 同号同内容：幂等回放，不重复写
                return {"batch_no": batch_no, "status": "replayed",
                        "message": "批次号已接收，内容一致，幂等忽略",
                        "batch_id": existing["id"]}
            # 同号内容不一致：首条生效，本次留待核对
            review = self.repository.record_review(
                batch_no, item_id, existing["fingerprint"], fingerprint,
                json.dumps(payload, ensure_ascii=False), actor)
            self.repository.append_audit("batch_review", INGEST, review["id"], actor, {
                "batch_no": batch_no, "item_id": item_id,
                "reason": "同批次号内容不一致",
            })
            return {"batch_no": batch_no, "status": "needs_review",
                    "message": "同批次号已有不同内容的首条记录，本次内容留待核对",
                    "review_id": review["id"]}

        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("任务区已关闭，不能再补录设备记录")
        plan = self._build_batch_plan(item_id, batch_no, entries)
        plan["actor"] = actor
        try:
            result = self.repository.apply_batch(plan)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("台账冲突，整批未写入（可能为并发占用）") from exc
        self.repository.append_audit("batch_ingest", INGEST, result["id"], actor, {
            "batch_no": batch_no, "item_id": item_id,
            "entry_count": result["entry_count"],
            "tasks_created": result["tasks_created"],
            "occupations_created": result["occupations_created"],
        })
        return result

    def _load_task(self, item_id: int, task_ref: str) -> Dict[str, Any]:
        task = self.repository.get_task_by_ref(task_ref)
        if task is None:
            raise NotFoundError("任务不存在")
        if task["item_id"] != item_id:
            raise ValidationError("任务不属于当前任务区")
        return task

    def _build_batch_plan(self, item_id: int, batch_no: str,
                          entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        # ---- 主数据解析 ----
        codes = sorted({e["device_code"] for e in entries})
        devices = {c: self.repository.get_device_by_code(c) for c in codes}
        missing = sorted(c for c in codes if devices[c] is None)
        if missing:
            raise ValidationError("设备未登记：" + "、".join(missing))

        refs = sorted({e["task_ref"] for e in entries})
        tasks: Dict[str, Dict[str, Any]] = {}
        for ref in refs:
            task = self.repository.get_task_by_ref(ref)
            if task is not None:
                if task["item_id"] != item_id:
                    raise ValidationError(f"任务 {ref} 不属于当前任务区")
                tasks[ref] = task

        states = self.repository.occupation_states([devices[c]["id"] for c in codes])
        task_ref_by_id = {t["id"]: t["ref"] for t in self.repository.list_tasks()}
        device_by_id = {devices[c]["id"]: c for c in codes}
        # (device_code, task_ref) -> {'occupation_id','arrived','terminal'}
        pairs: Dict[tuple, Dict[str, Any]] = {}
        active: Dict[str, Dict[str, Any]] = {}
        for st in states:
            device_code = device_by_id[st["device_id"]]
            task_ref = task_ref_by_id.get(st["task_id"], str(st["task_id"]))
            pair = {"occupation_id": st["occupation_id"], "arrived": bool(st["arrived"]),
                    "terminal": bool(st["terminal"]), "active": st["status"] == "active"}
            pairs[(device_code, task_ref)] = pair
            if st["status"] == "active":
                active[device_code] = {"task_ref": task_ref, **pair}

        new_tasks: Dict[str, Dict[str, str]] = {}
        actions: List[Dict[str, Any]] = []
        # 批内模拟态：代码 -> 占用设备的任务ref（批内未撤离/移交）
        sim_active: Dict[str, str] = {}
        conflicts: List[Dict[str, str]] = []
        seq = 0

        for entry in entries:
            seq += 1
            kind = entry["kind"]
            code = entry["device_code"]
            ref = entry["task_ref"]
            action = {"kind": kind, "task_ref": ref, "device_code": code,
                      "device_id": devices[code]["id"], "seq": seq,
                      "detail": entry.get("detail", "")}

            if kind == ENTRY_DISPATCH:
                if ref in tasks and tasks[ref]["status"] != "open":
                    raise ConflictError(f"任务 {ref} 已结束，不能再派单")
                if sim_active.get(code) == ref:
                    raise ValidationError(
                        f"批次内设备 {code} 对任务 {ref} 重复派单")
                if code in sim_active:
                    conflicts.append({"device_code": code,
                                      "device_type": devices[code]["device_type"],
                                      "task_ref": sim_active[code]})
                    continue
                if code in active:
                    conflicts.append({"device_code": code,
                                      "device_type": devices[code]["device_type"],
                                      "task_ref": active[code]["task_ref"]})
                    continue
                if ref not in tasks and ref not in new_tasks:
                    new_tasks[ref] = {"ref": ref,
                                      "title": entry.get("task_title") or ref}
                key = f"new:{len(actions)}"
                action["occupation_key"] = key
                actions.append(action)
                sim_active[code] = ref
                pairs[(code, ref)] = {"occupation_id": key, "arrived": False,
                                      "terminal": False, "active": True}
                continue

            pair = pairs.get((code, ref))
            if pair is None:
                raise ValidationError(
                    f"设备 {code} 对任务 {ref} 没有派单，无法补录{entry_label(kind)}记录")
            if not pair.get("active", True):
                raise ConflictError(
                    f"设备 {code} 对任务 {ref} 的占用已结束，{entry_label(kind)}记录需核对")
            if kind == ENTRY_ARRIVAL:
                if pair["arrived"]:
                    raise ConflictError(f"设备 {code} 对任务 {ref} 已有到场记录")
                pair["arrived"] = True
            else:
                if not pair["arrived"]:
                    raise ValidationError(
                        f"设备 {code} 对任务 {ref} 尚未到场，不能补录{entry_label(kind)}")
                if pair["terminal"]:
                    raise ConflictError(
                        f"设备 {code} 对任务 {ref} 已有撤离或移交记录")
                pair["terminal"] = True
                if sim_active.get(code) == ref:
                    del sim_active[code]
            action["occupation_key"] = pair["occupation_id"]
            actions.append(action)

        if conflicts:
            # 设备被另一条未结束任务占用：说清冲突任务，整批不写
            raise ConflictError(dispatch_conflict_message(conflicts))

        return {
            "item_id": item_id, "batch_no": batch_no,
            "fingerprint": batch_fingerprint(entries), "entries": entries,
            "new_tasks": list(new_tasks.values()),
            "existing_tasks": [{"ref": r, "id": tasks[r]["id"]} for r in tasks],
            "actions": actions,
        }

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
