import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (STATES, TRANSITION_ROLES, batch_fingerprint,
                       entry_fingerprint)


FC = "field_commander"
IC = "incident_commander"
LOG = "logistics"


def dispatch(device_code, task_ref, task_title=None):
    e = {"kind": "dispatch", "device_code": device_code, "task_ref": task_ref}
    if task_title:
        e["task_title"] = task_title
    return e


def arrival(device_code, task_ref):
    return {"kind": "arrival", "device_code": device_code, "task_ref": task_ref}


def evacuation(device_code, task_ref):
    return {"kind": "evacuation", "device_code": device_code, "task_ref": task_ref}


def handover(device_code, task_ref, receiver=None):
    e = {"kind": "handover", "device_code": device_code, "task_ref": task_ref}
    if receiver:
        e["receiver"] = receiver
    return e


class DeviceBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "山谷火场", "description": "离线补录", "severity": "high",
             "quantity": 3, "threshold": 2, "external_ref": "WF-DEV-1"},
            "creator", FC)
        for code, name, dtype in (("PUMP-1", "1号水泵组", "pump"),
                                  ("SAW-1", "1号油锯", "chainsaw"),
                                  ("BUS-1", "1号运兵车", "troop_carrier")):
            self.service.register_device(
                {"code": code, "name": name, "device_type": dtype},
                "quartermaster", LOG)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _ingest(self, batch_no, entries, role=FC, actor="line"):
        return self.service.ingest_batch(self.item["id"],
                                         {"batch_no": batch_no, "entries": entries},
                                         actor, role)

    def test_batch_first_wins_idempotent_and_review(self):
        entries = [dispatch("PUMP-1", "T-1", "东线压制"),
                   arrival("PUMP-1", "T-1"),
                   dispatch("SAW-1", "T-2", "清理通道"),
                   arrival("SAW-1", "T-2"),
                   handover("SAW-1", "T-2", receiver="二班")]
        result = self._ingest("B-1", entries)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["tasks_created"], 2)
        self.assertEqual(result["occupations_created"], 2)
        # 同号同内容重传：幂等，不重复写
        replay = self._ingest("B-1", entries)
        self.assertEqual(replay["status"], "replayed")
        records = self.service.list_device_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 5)
        # 同号不同内容：留待核对，首条仍然生效
        changed = [dispatch("PUMP-1", "T-1", "西线巡护"),  # task_title 变了
                   arrival("PUMP-1", "T-1")]
        self.assertNotEqual(batch_fingerprint(entries), batch_fingerprint(changed))
        review_resp = self._ingest("B-1", changed)
        self.assertEqual(review_resp["status"], "needs_review")
        reviews = self.service.list_reviews("viewer")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["batch_no"], "B-1")
        # 台账未被新内容改写
        tasks = {t["ref"]: t for t in self.service.list_tasks(self.item["id"], "viewer")}
        self.assertEqual(tasks["T-1"]["title"], "东线压制")
        records = self.service.list_device_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 5)

    def test_occupied_device_blocks_whole_batch_with_conflict_task(self):
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "东线"),
                             arrival("PUMP-1", "T-1")])
        # PUMP-1 仍在未结束任务 T-1 上；批内还有另一台合法设备 → 整批不写
        conflicting = [dispatch("PUMP-1", "T-2", "西线"),
                       dispatch("BUS-1", "T-2", "运兵")]
        with self.assertRaises(ConflictError) as ctx:
            self._ingest("B-2", conflicting)
        self.assertIn("T-1", str(ctx.exception))
        self.assertIn("整批不写", str(ctx.exception))
        # 整批回滚：BUS-1 也没有被派单，T-2 未建
        self.assertEqual(len(self.service.list_tasks(self.item["id"], "viewer")), 1)
        occupations = self.service.list_occupations(self.item["id"], "viewer")
        self.assertEqual(len(occupations), 1)
        self.assertEqual(occupations[0]["device_code"], "PUMP-1")

    def test_released_device_can_be_redispatched(self):
        self._ingest("B-1", [dispatch("SAW-1", "T-1", "清理"),
                             arrival("SAW-1", "T-1"),
                             evacuation("SAW-1", "T-1")])
        self._ingest("B-2", [dispatch("SAW-1", "T-2", "复燃点"),
                             arrival("SAW-1", "T-2")])
        occs = self.service.list_occupations(self.item["id"], "viewer")
        self.assertEqual([o["status"] for o in occs], ["released", "active"])

    def test_arrival_without_dispatch_and_terminal_without_arrival(self):
        with self.assertRaises(ValidationError):
            self._ingest("B-X", [arrival("PUMP-1", "T-9")])
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "东线")])
        with self.assertRaises(ValidationError):
            self._ingest("B-2", [handover("PUMP-1", "T-1", receiver="二班")])
        with self.assertRaises(ConflictError):
            self._ingest("B-3", [dispatch("PUMP-1", "T-1", "东线")])

    def test_close_invariant_evacuation_or_handover_per_device(self):
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "东线"),
                             arrival("PUMP-1", "T-1"),
                             dispatch("SAW-1", "T-1", "东线"),
                             arrival("SAW-1", "T-1"),
                             dispatch("BUS-1", "T-2", "转运"),
                             arrival("BUS-1", "T-2")])
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "ic",
                TRANSITION_ROLES[target][0])
        # 还有设备缺撤离/移交 → 关闭被拒，且点名设备
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "ic", IC)
        message = str(ctx.exception)
        self.assertIn("PUMP-1", message)
        self.assertIn("BUS-1", message)
        # 补齐：水泵组撤离、油锯移交、运兵车撤离后可关闭
        self._ingest("B-2", [evacuation("PUMP-1", "T-1"),
                             handover("SAW-1", "T-1", receiver="二班"),
                             evacuation("BUS-1", "T-2")])
        current = self.service.get_item(self.item["id"], "viewer")
        closed = self.service.transition(current["id"], "closed",
                                         current["version"], "ic", IC)
        self.assertEqual(closed["status"], "closed")

    def test_finish_task_requires_clearance(self):
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "东线"),
                             arrival("PUMP-1", "T-1")])
        with self.assertRaises(ConflictError):
            self.service.finish_task(self.item["id"], "T-1", "fc", FC)
        self._ingest("B-2", [evacuation("PUMP-1", "T-1")])
        finished = self.service.finish_task(self.item["id"], "T-1", "fc", FC)
        self.assertEqual(finished["status"], "finished")
        with self.assertRaises(ConflictError):
            self._ingest("B-3", [dispatch("SAW-1", "T-1", "收尾")])

    def test_roles_and_audit_chain(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_device(
                {"code": "X", "name": "x", "device_type": "pump"}, "a", "viewer")
        with self.assertRaises(PermissionDenied):
            self._ingest("B-X", [dispatch("PUMP-1", "T-1")], role="viewer")
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "东线"),
                             arrival("PUMP-1", "T-1"),
                             handover("PUMP-1", "T-1", receiver="二班")])
        self.service.register_device(
            {"code": "PUMP-2", "name": "2号水泵组", "device_type": "pump"},
            "q", FC)
        actions = [e["action"] for e in self.service.audit(IC)]
        self.assertIn("batch_ingest", actions)
        self.assertIn("device_register", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_review_audit_on_divergent(self):
        entries = [dispatch("PUMP-1", "T-1", "东线"), arrival("PUMP-1", "T-1")]
        self._ingest("B-1", entries)
        self._ingest("B-1", [dispatch("PUMP-1", "T-1", "南线")])
        actions = [e["action"] for e in self.service.audit(IC)]
        self.assertIn("batch_review", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_fingerprint_helpers(self):
        a = dispatch("PUMP-1", "T-1", "东线")
        b = dispatch("PUMP-1", "T-1", "东线")
        c = dispatch("PUMP-1", "T-1", "西线")
        self.assertEqual(entry_fingerprint(a), entry_fingerprint(b))
        self.assertNotEqual(entry_fingerprint(a), entry_fingerprint(c))

    def test_unknown_device_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._ingest("B-1", [dispatch("NOPE-9", "T-1", "东线")])
        self.assertIn("NOPE-9", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
