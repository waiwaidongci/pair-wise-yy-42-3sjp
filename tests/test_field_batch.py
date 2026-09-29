import tempfile, unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

FC = 'field_commander'
IC = 'incident_commander'
LOG = 'logistics'


class FieldBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.zone = self.service.create_item(
            {"title": "东线任务区", "description": "水泵作业", "severity": "high",
             "quantity": 3, "threshold": 2, "external_ref": "ZONE-A"}, "cmd", FC)
        self.other = self.service.create_item(
            {"title": "西线任务区", "description": "运兵", "severity": "moderate",
             "quantity": 1, "threshold": 1, "external_ref": "ZONE-B"}, "cmd", FC)
        self.service.register_equipment(
            {"code": "PUMP-01", "kind": "pump", "label": "水泵组一号"}, "log", LOG)
        self.service.register_equipment(
            {"code": "SAW-01", "kind": "chainsaw", "label": "油锯一号"}, "log", LOG)
        self.service.register_equipment(
            {"code": "VAN-01", "kind": "transport", "label": "运兵车一号"}, "log", LOG)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def batch(self, zone, batch_no, entries, actor="front", role=FC):
        return self.service.ingest_field_batch(
            zone["id"], {"batch_no": batch_no, "entries": entries}, actor, role)

    def close_zone(self, zone):
        current = self.service.get_item(zone["id"], "viewer")
        remaining = STATES[STATES.index(current["status"]) + 1:]
        for target in remaining:
            current = self.service.transition(
                current["id"], target, current["version"], "ic",
                TRANSITION_ROLES[target][0])
        return current

    def test_full_cycle_arrival_dispatch_evacuation_then_close(self):
        result = self.batch(self.zone, "B-1", [
            {"equipment_code": "PUMP-01", "op": "arrival", "detail": {"ts": "08:00"}},
            {"equipment_code": "PUMP-01", "op": "dispatch"},
        ])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["applied_count"], 2)

        ledger = self.service.field_ledger(self.zone["id"], "viewer")
        self.assertEqual(len(ledger["open_occupations"]), 1)
        self.assertEqual(len(ledger["arrivals_open"]), 1)
        self.assertTrue(any("PUMP-01" in b for b in ledger["blockers"]))

        # 推进到 controlled 后 closed 被设备台账拦截
        current = self.service.get_item(self.zone["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "ic", IC)
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "ic", IC)

        # 补录撤离记录后可以关闭
        self.batch(self.zone, "B-2", [
            {"equipment_code": "PUMP-01", "op": "evacuation"}])
        ledger = self.service.field_ledger(self.zone["id"], "viewer")
        self.assertEqual(ledger["blockers"], [])
        self.close_zone(self.zone)

    def test_same_batch_id_first_wins_identical_is_idempotent(self):
        payload_entries = [{"equipment_code": "SAW-01", "op": "arrival"}]
        first = self.batch(self.zone, "B-DUP", payload_entries)
        self.assertEqual(first["status"], "applied")
        again = self.batch(self.zone, "B-DUP",
                           [{"equipment_code": "SAW-01", "op": "arrival"}])
        self.assertEqual(again["status"], "duplicate")
        # 只落了一次到场
        ledger = self.service.field_ledger(self.zone["id"], "viewer")
        self.assertEqual(len(ledger["arrivals_open"]), 1)

    def test_same_batch_id_content_mismatch_held_for_review(self):
        self.batch(self.zone, "B-MIS", [
            {"equipment_code": "SAW-01", "op": "arrival"}])
        with self.assertRaises(ConflictError):
            self.batch(self.zone, "B-MIS", [
                {"equipment_code": "SAW-01", "op": "arrival",
                 "detail": {"note": "different"}}])
        pending = self.service.list_mismatches(IC)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["batch_no"], "B-MIS")
        self.assertEqual(pending[0]["status"], "pending")

    def test_occupied_by_other_task_rejects_entire_batch(self):
        # 西线任务区已占用运兵车
        self.batch(self.other, "O-1", [
            {"equipment_code": "VAN-01", "op": "arrival"},
            {"equipment_code": "VAN-01", "op": "dispatch"}])
        # 东线再派 VAN-01：错误要说清冲突任务，且整批不写
        try:
            self.batch(self.zone, "O-2", [
                {"equipment_code": "PUMP-01", "op": "dispatch"},
                {"equipment_code": "VAN-01", "op": "dispatch"}])
            self.fail("应当冲突")
        except ConflictError as exc:
            self.assertIn(str(self.other["id"]), str(exc))
            self.assertIn("整批未写入", str(exc))
        # PUMP-01 也没有被写入
        self.assertEqual(self.service.field_ledger(self.zone["id"], "viewer")
                         ["open_occupations"], [])
        # 批次标记为 rejected，修正后可按同号重传
        retry = self.batch(self.zone, "O-2", [
            {"equipment_code": "PUMP-01", "op": "arrival"},
            {"equipment_code": "PUMP-01", "op": "dispatch"}])
        self.assertEqual(retry["status"], "applied")
        self.assertEqual(retry["applied_count"], 2)

    def test_handover_releases_source_and_occupies_target(self):
        self.batch(self.zone, "H-1", [
            {"equipment_code": "VAN-01", "op": "arrival"},
            {"equipment_code": "VAN-01", "op": "dispatch"}])
        result = self.batch(self.zone, "H-2", [
            {"equipment_code": "VAN-01", "op": "handover",
             "to_item_id": self.other["id"]}])
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(self.service.field_ledger(self.zone["id"], "viewer")
                         ["blockers"], [])
        target = self.service.field_ledger(self.other["id"], "viewer")
        self.assertEqual(len(target["open_occupations"]), 1)
        self.assertTrue(any("VAN-01" in b for b in target["blockers"]))

    def test_duplicate_dispatch_same_task_is_idempotent_skip(self):
        self.batch(self.zone, "D-1", [
            {"equipment_code": "PUMP-01", "op": "dispatch"}])
        result = self.batch(self.zone, "D-2", [
            {"equipment_code": "PUMP-01", "op": "dispatch"}])
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(self.service.field_ledger(self.zone["id"], "viewer")
                            ["open_occupations"]), 1)

    def test_unknown_equipment_rejects_batch(self):
        with self.assertRaises(ConflictError):
            self.batch(self.zone, "X-1", [
                {"equipment_code": "NOPE", "op": "arrival"}])

    def test_handover_requires_open_target(self):
        self.batch(self.zone, "C-1", [
            {"equipment_code": "SAW-01", "op": "arrival"},
            {"equipment_code": "SAW-01", "op": "dispatch"}])
        closed = self.close_zone(self.other)
        with self.assertRaises(ConflictError):
            self.batch(self.zone, "C-2", [
                {"equipment_code": "SAW-01", "op": "handover",
                 "to_item_id": closed["id"]}])

    def test_viewer_cannot_ingest(self):
        with self.assertRaises(PermissionDenied):
            self.batch(self.zone, "P-1", [
                {"equipment_code": "PUMP-01", "op": "arrival"}], role="viewer")

    def test_audit_chain_intact(self):
        self.batch(self.zone, "A-1", [
            {"equipment_code": "PUMP-01", "op": "arrival"},
            {"equipment_code": "PUMP-01", "op": "dispatch"}])
        self.assertTrue(self.repo.verify_audit_chain())
        actions = {e["action"] for e in self.repo.list_audit()}
        self.assertIn("field_batch_applied", actions)
        self.assertIn("equipment_register", actions)


if __name__ == "__main__":
    unittest.main()
