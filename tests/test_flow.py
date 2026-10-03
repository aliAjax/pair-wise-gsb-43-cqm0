import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (  # noqa: E402
    BATCH_FAILED,
    BATCH_OPENED,
    BATCH_PENDING,
    BATCH_UNWITNESSED,
    DomainError,
    ProcurementService,
)


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

    def open_flow(self, version=None, supervisor="sup1", auditor="aud1"):
        """采购员发起批次 -> 监标人核对 -> 审计员核对，返回批次视图。"""
        version = self.tender["version"] if version is None else version
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"], version)
        sup_view = self.service.confirm_open_batch(supervisor, "supervisor", batch["id"])
        self.assertEqual(BATCH_PENDING, sup_view["status"])
        aud_view = self.service.confirm_open_batch(auditor, "auditor", batch["id"])
        self.assertEqual(BATCH_OPENED, aud_view["status"])
        return aud_view

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        batch = self.open_flow()
        self.assertEqual(2, len(batch["items"]))
        self.assertEqual({"supervisor": "sup1", "auditor": "aud1"}, batch["confirmers"])
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", detail["tender"]["status"])
        self.assertEqual(batch["batch_no"], detail["tender"]["open_batch_no"])
        for row in detail["bids"]:
            self.assertEqual(batch["batch_no"], row["open_batch_no"])
            self.assertEqual("已见证", row["witness_status"])
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(bid1["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(batch["batch_no"], award["award"]["open_batch_no"])

    def test_requires_supervisor_and_auditor_separate_confirmation(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        time.sleep(2.1)
        # 只有采购员不能直接开标
        with self.assertRaises(DomainError) as ctx:
            self.service.start_open_batch("vendor1", "vendor", self.tender["id"], self.tender["version"])
        self.assertEqual(403, ctx.exception.status)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"], self.tender["version"])
        # 监标人一人核对后项目仍未开标
        sup_view = self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual(BATCH_PENDING, sup_view["status"])
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("published", detail["tender"]["status"])
        self.assertEqual("sealed", detail["bids"][0]["status"])
        # 采购员不能充当审计员
        with self.assertRaises(DomainError) as ctx2:
            self.service.confirm_open_batch("proc1", "procurement", batch["id"])
        self.assertEqual(403, ctx2.exception.status)
        # 审计员核对后统一开标
        aud_view = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual(BATCH_OPENED, aud_view["status"])

    def test_confirmed_party_is_idempotent_and_not_reprocessed(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"], self.tender["version"])
        first = self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual(1, first["attempts"])
        # 监标人重复核对：幂等跳过，不重复处理
        again = self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual(BATCH_PENDING, again["status"])
        self.assertEqual("sup1", again["supervisor_confirmed_by"])
        # 另一人不能冒用监标人身份重复确认
        with self.assertRaises(DomainError):
            self.service.confirm_open_batch("sup2", "supervisor", batch["id"])
        opened = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual(BATCH_OPENED, opened["status"])
        # 已开标的批次任何重复核对都被拒绝，投标状态不重复变化
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual(409, ctx.exception.status)
        with self.service.connect() as conn:
            row = conn.execute("SELECT version FROM bids WHERE id=?", (bid["id"],)).fetchone()
            self.assertEqual(2, row["version"])

    def test_hash_mismatch_halts_whole_batch_and_blocks_evaluation_and_award(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B5", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B6", 700000, 80)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"], self.tender["version"])
        # 篡改投标正文但承诺哈希不变：核对必须发现承诺哈希不一致
        import sqlite3

        with sqlite3.connect(self.db_path) as raw:
            raw.execute("UPDATE bids SET payload=? WHERE id=?", (json.dumps({"报价": 1, "质量": 1}, ensure_ascii=False), bid1["id"]))
        result = self.service.confirm_open_batch("sup1", "supervisor", batch["id"])
        self.assertEqual(BATCH_FAILED, result["status"])
        self.assertIn("承诺哈希", result["error"])
        self.assertEqual(1, len(result["failure_detail"]))
        self.assertEqual(bid1["id"], result["failure_detail"][0]["bid_id"])
        # 整批停在待核：所有投标仍是 sealed，项目仍是 published
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("published", detail["tender"]["status"])
        self.assertEqual(batch["batch_no"], detail["tender"]["open_batch_no"])
        self.assertIn("承诺哈希不符", detail["tender"]["open_batch_failure_reason"])
        self.assertTrue(all(row["status"] == "sealed" for row in detail["bids"]))
        self.assertTrue(all(row["witness_status"] == "核对失败" for row in detail["bids"]))
        # 审计员也无法让失败批次继续
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual(409, ctx.exception.status)
        # 失败批次不得进入评分或授标
        with self.assertRaises(DomainError) as ctx_eval:
            self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx_eval.exception.status)
        with self.assertRaises(DomainError) as ctx_award:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self.tender["version"])
        self.assertEqual(409, ctx_award.exception.status)
        # 失败批次未解决前不能重新发起
        with self.assertRaises(DomainError) as ctx_restart:
            self.service.start_open_batch("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(409, ctx_restart.exception.status)

    def test_confirm_write_failure_rolls_back_and_allows_retry_without_reprocessing(self):
        self.bid(self.vendor1, "vendor1", "B7", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B8", 700000, 80)
        time.sleep(2.1)
        batch = self.service.start_open_batch("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.confirm_open_batch("sup1", "supervisor", batch["id"])

        # 注入写入失败：审计员核对写入过程中抛错，事务必须整体回滚到未开标
        original_connect = self.service.connect

        class FailError(Exception):
            pass

        class FailingConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("UPDATE open_batches SET auditor_confirmed_by"):
                    raise FailError("模拟写入失败")
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

        self.service.connect = lambda: FailingConn(original_connect())
        with self.assertRaises(FailError):
            self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.service.connect = original_connect

        # 回滚后：投标仍 sealed、批次仍待核、监标人确认仍保留
        view = self.service.get_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual(BATCH_PENDING, view["status"])
        self.assertEqual("sup1", view["supervisor_confirmed_by"])
        self.assertIsNone(view["auditor_confirmed_by"])
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("published", detail["tender"]["status"])
        self.assertTrue(all(row["status"] == "sealed" for row in detail["bids"]))

        # 重试：监标人确认保留无需重做，审计员重新核对即可完成开标
        retried = self.service.confirm_open_batch("aud1", "auditor", batch["id"])
        self.assertEqual(BATCH_OPENED, retried["status"])
        self.assertEqual({"supervisor": "sup1", "auditor": "aud1"}, retried["confirmers"])
        final = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("opened", final["tender"]["status"])
        self.assertTrue(all(row["status"] == "opened" for row in final["bids"]))

    def test_audit_timeline_records_same_batch_no_and_confirmers(self):
        self.bid(self.vendor1, "vendor1", "B9", 800000, 90)
        time.sleep(2.1)
        batch = self.open_flow()
        state = self.service.state("aud1", "auditor")
        actions = {row["action"]: json.loads(row["details"]) for row in state["timeline"]
                   if "open_batch" in row["action"]}
        self.assertEqual(batch["batch_no"], actions["open_batch.initiated"]["batch_no"])
        self.assertEqual(batch["batch_no"], actions["open_batch.confirmed"]["batch_no"])
        self.assertEqual(batch["batch_no"], actions["open_batch.opened"]["batch_no"])
        self.assertEqual({"supervisor": "sup1", "auditor": "aud1"}, actions["open_batch.opened"]["confirmers"])
        # 公开页（public 角色）也能看到同一批次号和确认人
        public = self.service.state()
        entry = next(t for t in public["tenders"] if t["id"] == self.tender["id"])
        self.assertEqual(batch["batch_no"], entry["open_batch_no"])
        self.assertEqual({"supervisor": "sup1", "auditor": "aud1"}, entry["open_batch_confirmers"])
        self.assertIn(batch["batch_no"], {b["batch_no"] for b in public["open_batches"]})

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.open_flow()
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.open_flow()
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
            self.service.start_open_batch("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)


class LegacyBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_historical_opened_bids_without_batch_are_backfilled_unwitnessed(self):
        import sqlite3

        # 用旧版结构（无批次表/列）手工写入一条已开标历史数据
        with sqlite3.connect(self.db_path) as raw:
            raw.executescript(
                """
                CREATE TABLE tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tender_no TEXT UNIQUE, title TEXT,
                    description TEXT DEFAULT '', status TEXT DEFAULT 'draft', deadline TEXT,
                    criteria TEXT DEFAULT '[]', evaluation_round INTEGER DEFAULT 1,
                    evaluations_locked INTEGER DEFAULT 0, awarded_bid_id INTEGER, award_snapshot TEXT,
                    version INTEGER DEFAULT 1, created_by TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_no TEXT UNIQUE, name TEXT,
                    representative TEXT, created_at TEXT
                );
                CREATE TABLE bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id INTEGER, vendor_id INTEGER,
                    payload TEXT, payload_hash TEXT, price REAL, status TEXT DEFAULT 'sealed',
                    version INTEGER DEFAULT 1, submitted_by TEXT, submitted_at TEXT, opened_at TEXT
                );
                """
            )
            now = datetime.now(timezone.utc).isoformat()
            raw.execute(
                "INSERT INTO tenders(tender_no,title,status,deadline,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("T-OLD", "历史项目", "opened", now, "proc-old", now, now),
            )
            raw.execute(
                "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                ("V-OLD", "旧供应商", "v-old", now),
            )
            raw.execute(
                "INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,status,submitted_by,submitted_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (1, 1, '{"报价": 100}', "deadbeef", 100.0, "opened", "v-old", now, now),
            )
        # 打开服务触发迁移：历史投标按未见证回填
        service = ProcurementService(self.db_path)
        detail = service.get_tender("aud1", "auditor", 1)
        self.assertEqual("未见证", detail["bids"][0]["witness_status"])
        self.assertTrue(detail["bids"][0]["open_batch_no"].startswith("LEGACY-"))
        self.assertEqual(BATCH_UNWITNESSED, detail["tender"]["open_batch_status"])
        self.assertIn("历史", detail["tender"]["open_batch_failure_reason"])
        batch = service.state("aud1", "auditor")
        legacy = next(b for b in batch["open_batches"] if b["tender_id"] == 1)
        self.assertEqual(BATCH_UNWITNESSED, legacy["status"])
        self.assertIsNone(legacy["confirmers"]["supervisor"])
        self.assertIsNone(legacy["confirmers"]["auditor"])
        events = [row for row in service.state()["timeline"] if row["action"] == "open_batch.backfilled"]
        self.assertEqual(1, len(events))
        # 未见证回填不阻止后续评分（历史流程已完成开标）
        service.evaluate_bid("eval-old", "evaluator", 1, {"报价": 100})


if __name__ == "__main__":
    unittest.main()
