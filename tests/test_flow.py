import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ProcurementService, DomainError  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = ProcurementService(self.db_path)
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def open_witnessed(self, price1=800000, quality1=90, price2=700000, quality2=80):
        b1 = self.bid(self.vendor1, "vendor1", "B1", price1, quality1)
        b2 = self.bid(self.vendor2, "vendor2", "B2", price2, quality2)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        return b1, b2, self.service.get_open_batch("aud1", "auditor", batch["id"])

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.assertEqual("pending", batch["status"])
        self.assertEqual(2, len(batch["items"]))
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        still_pending = self.service.get_open_batch("proc1", "procurement", batch["id"])
        self.assertEqual("pending", still_pending["status"])
        # 监标人单方核对通过不得开标
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertTrue(all(b["status"] == "sealed" for b in detail["bids"]))
        self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        opened = self.service.get_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual("witnessed", opened["status"])
        self.assertEqual("sup1", opened["supervisor_confirmed_by"])
        self.assertEqual("aud1", opened["auditor_confirmed_by"])
        self.assertTrue(all(i["bid_status"] == "opened" for i in opened["items"]))
        bid_ids = sorted(i["bid_id"] for i in opened["items"])
        self.service.evaluate_bid("eval1", "evaluator", bid_ids[0], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid_ids[0], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid_ids[1], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(bid_ids[0], award["award"]["winner"]["bid_id"])

    def test_batch_is_idempotent_for_already_confirmed_party(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        # 同一确认人重复提交，已确认部分不重复处理
        again = self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual("pending", again["status"])
        self.assertEqual("sup1", again["supervisor_confirmed_by"])
        self.assertIsNone(again["auditor_confirmed_by"])
        self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        witnessed = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual("witnessed", witnessed["status"])
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual(2, detail["bids"][0]["version"])
        # 开标已统一处理后再重复核对，不再重复处理、版本不再增长
        self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        detail_again = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual(2, detail_again["bids"][0]["version"])

    def test_hash_discrepancy_holds_whole_batch_and_records_reason(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        # 审计员核对前，投标正文被篡改（承诺哈希不再匹配）
        import sqlite3
        with sqlite3.connect(self.db_path) as raw:
            raw.execute(
                "UPDATE bids SET payload=? WHERE tender_id=?",
                (json.dumps({"报价": 1, "质量": 1}, ensure_ascii=False), self.tender["id"]),
            )
        result = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertFalse(result["ok"])
        self.assertEqual("disputed", result["status"])
        self.assertEqual(2, result["discrepancy_count"])
        self.assertIn("审计员", result["failure_reason"])
        # 整批停在待核，没有任何投标被开标
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("published", detail["tender"]["status"])
        self.assertTrue(all(b["status"] == "sealed" for b in detail["bids"]))
        # 失败批次不得进入评分或授标
        any_bid = detail["bids"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", any_bid, {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(409, ctx2.exception.status)
        # 待核/失败批次未恢复前不能重开批次
        with self.assertRaises(DomainError) as ctx3:
            self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.assertEqual(409, ctx3.exception.status)

        # 排除差异（恢复正确正文）后由采购员恢复批次，两方重新核对，统一开标成功
        import sqlite3
        original_payload = json.dumps({"报价": 800000, "质量": 90}, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.db_path) as raw:
            raw.execute("UPDATE bids SET payload=? WHERE vendor_id=?", (original_payload, self.vendor1["id"]))
            raw.execute("UPDATE bids SET payload=? WHERE vendor_id=?",
                        (json.dumps({"报价": 700000, "质量": 80}, ensure_ascii=False, sort_keys=True), self.vendor2["id"]))
        recovered = self.service.recover_open_batch("proc1", "procurement", batch["id"], "正文已更正")
        self.assertEqual("pending", recovered["status"])
        self.assertEqual(0, recovered["discrepancy_count"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        done = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual("witnessed", done["status"])
        detail2 = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("opened", detail2["tender"]["status"])
        self.assertTrue(all(b["status"] == "opened" for b in detail2["bids"]))
        self.assertTrue(all(b["witness_status"] == "witnessed" for b in detail2["bids"]))

    def test_rollback_to_sealed_after_failed_write_can_retry(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])

        # 模拟第二方核对通过后的统一开标写入失败：整个事务回滚，投标仍为未开标
        import app as app_module
        original = app_module.ProcurementService._finalize_open_batch

        def failing_finalize(self, conn, fresh, items):
            original(self, conn, fresh, items)
            raise RuntimeError("simulated write failure")

        app_module.ProcurementService._finalize_open_batch = failing_finalize
        try:
            with self.assertRaises(RuntimeError):
                self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        finally:
            app_module.ProcurementService._finalize_open_batch = original

        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("sealed", detail["bids"][0]["status"])
        held = self.service.get_open_batch("proc1", "procurement", batch["id"])
        self.assertIn(held["status"], {"pending", "disputed"})

        # 恢复后重试：两方核对记录保留，统一开标成功且不重复处理
        retried = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual("witnessed", retried["status"])
        self.assertEqual("sup1", retried["supervisor_confirmed_by"])
        self.assertEqual("aud1", retried["auditor_confirmed_by"])
        after = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("opened", after["bids"][0]["status"])

    def test_only_supervisor_and_auditor_can_confirm(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_open_batch("proc1", "procurement", batch["id"])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.confirm_open_batch("vendor1", "vendor", batch["id"])
        self.assertEqual(403, ctx2.exception.status)

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid, _, _ = self.open_witnessed()
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid, _, _ = self.open_witnessed()
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.start_open_batch("vendor1", "vendor", self.tender["id"])
        self.assertEqual(403, ctx.exception.status)

    def test_batch_no_and_confirmer_visible_in_detail_public_and_audit(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        batch_no = batch["batch_no"]

        detail = self.service.get_tender("public", "public", self.tender["id"])
        self.assertEqual(batch_no, detail["open_batch"]["batch_no"])
        self.assertEqual("sup1", detail["open_batch"]["supervisor_confirmed_by"])
        self.assertEqual("aud1", detail["open_batch"]["auditor_confirmed_by"])
        self.assertTrue(detail["bids"])
        self.assertEqual(batch_no, detail["bids"][0]["open_batch_no"])
        self.assertEqual("witnessed", detail["bids"][0]["witness_status"])

        state = self.service.state("pub", "public")
        entry = next(t for t in state["tenders"] if t["id"] == self.tender["id"])
        self.assertEqual(batch_no, entry["open_batch"]["batch_no"])
        self.assertEqual("sup1", entry["open_batch"]["supervisor_confirmed_by"])
        state_bid = next(b for b in state["bids"] if b["tender_id"] == self.tender["id"])
        self.assertEqual(batch_no, state_bid["open_batch_no"])

        # 审计记录带同一批次号和确认人
        opened_event = next(e for e in state["timeline"] if e["action"] == "tender.opened")
        self.assertEqual(batch_no, json.loads(opened_event["details"])["batch_no"])
        confirmed_events = [e for e in state["timeline"] if e["action"] == "open_batch.confirmed"]
        self.assertEqual({"supervisor", "auditor"},
                         {json.loads(e["details"])["party"] for e in confirmed_events})

    def test_legacy_opened_bids_backfilled_as_unwitnessed_and_blocked_from_scoring(self):
        # 用旧版逻辑手工构造"已开标但无批次"的历史数据
        import sqlite3
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        with sqlite3.connect(self.db_path) as raw:
            raw.execute("UPDATE tenders SET status='opened' WHERE id=?", (self.tender["id"],))
            raw.execute(
                "UPDATE bids SET status='opened',opened_at=? WHERE tender_id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), self.tender["id"]),
            )

        # 重新初始化服务触发历史回填
        migrated = ProcurementService(self.db_path)
        detail = migrated.get_tender("aud1", "auditor", self.tender["id"])
        self.assertEqual("unwitnessed", detail["open_batch"]["status"])
        self.assertTrue(detail["open_batch"]["batch_no"].startswith("UNWITNESSED-"))
        self.assertIn("未见证", detail["open_batch"]["failure_reason"])
        self.assertEqual("unwitnessed", detail["bids"][0]["witness_status"])

        # 历史未见证数据不得进入评分或授标
        with self.assertRaises(DomainError) as ctx:
            migrated.evaluate_bid("eval1", "evaluator", detail["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            migrated.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(409, ctx2.exception.status)

        # 回填幂等：再次初始化不重复生成批次
        ProcurementService(self.db_path)
        state = ProcurementService(self.db_path).state("proc1", "procurement")
        batch_events = [e for e in state["timeline"] if e["action"] == "open_batch.backfilled"]
        self.assertEqual(1, len(batch_events))


if __name__ == "__main__":
    unittest.main()
