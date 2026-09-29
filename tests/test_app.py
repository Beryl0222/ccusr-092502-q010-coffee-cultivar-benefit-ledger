"""权益分配账本的业务不变量测试。"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.commands import CommandError, DuplicateCommand, LedgerApp
from tests.fixtures import build_app, seed_full_chain


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app: LedgerApp = build_app(str(Path(self.tmp.name) / "events.jsonl"))
        seed_full_chain(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _post(self, eid: str = "s-1") -> None:
        self.app.post_settlement(eid, "2026-09-28T09:00:00+08:00",
                                 "ST-1", "window-1", "settler-1")

    def line(self, bid: str) -> Decimal:
        return next(l.amount for l in self.app.ledger.settlements["ST-1"]["lines"]
                    if l.beneficiary_id == bid)

    # ---- 职务互斥 / 代签 ----

    def test_duty_roles_are_mutually_exclusive(self) -> None:
        with self.assertRaises(CommandError):
            self.app.register_party("bad-1", "2026-09-01T09:00:00+08:00",
                                    "bad", "兼任", ["ALLIANCE"],
                                    ["INSPECTOR", "SETTLER"])

    def test_inspector_cannot_sign_license(self) -> None:
        with self.assertRaises(CommandError):
            self.app.grant_license("x", "2026-09-01T09:00:00+08:00", "license-x",
                                   "YUNKA-1", "v1", "inspector-1", "nursery-1", {})

    def test_settler_cannot_grade(self) -> None:
        with self.assertRaises(CommandError):
            self.app.grade_lot("x", "2026-09-24T09:00:00+08:00", "insp-x",
                               "sale-a", "GRADE_A", "settler-1")

    # ---- 幂等 ----

    def test_command_resend_is_idempotent(self) -> None:
        self._post()
        with self.assertRaises(DuplicateCommand):
            self._post()
        self.assertEqual(len([s for s in self.app.ledger.settlements]), 1)

    def test_receipt_number_unique(self) -> None:
        with self.assertRaises(CommandError):
            self.app.deliver_cherry("e-d1-dup", "2026-09-20T09:00:00+08:00", "R-001",
                                    "plot-a", "farmer-a", "buy-1", "1",
                                    "2026-09-20T09:00:00+08:00")

    # ---- 结算金额与绿色扣减 ----

    def test_settlement_amounts_and_green_penalty(self) -> None:
        self._post()
        # A 户：(900*1 + 380*0.6)*4*0.625 摊配 A 占 62.5%
        # A 的 premium kg 加权：special 562.5 + gradeA 237.5
        # raw_A = (562.5*4*1 + 237.5*4*0.6) = 2250 + 570 = 2820；绿色齐全
        # farmer = 2820*0.7 = 1974
        self.assertEqual(self.line("farmer-a"), Decimal("1974.00"))
        # B：raw = (337.5*4 + 142.5*4*0.6)=1350+342=1692；绿色半额
        # farmer = 1692*0.5*0.7 = 592.2
        self.assertEqual(self.line("farmer-b"), Decimal("592.20"))
        st = self.app.ledger.settlements["ST-1"]
        self.assertEqual(st["total"], Decimal("5120.00"))

    def test_total_cannot_exceed_frozen(self) -> None:
        # 规则比例合计必须为 1
        with self.assertRaises(CommandError):
            self.app.publish_rule(
                "rule-bad", "2026-09-25T09:00:00+08:00", "rule-bad", 1,
                shares={"FARMER": "0.9", "COOPERATIVE": "0.2"},
                grade_rates={"SPECIAL": "1", "GRADE_A": "1", "REJECT": "0"},
                settler_id="settler-1")

    def test_freeze_total_must_match_quantity_times_rate(self) -> None:
        self.app.open_sale_window("w2", "2026-09-26T09:00:00+08:00",
                                  "window-2", "brand-1", "rule-1")
        with self.assertRaises(CommandError):
            self.app.freeze_premium(
                "f-bad", "2026-09-27T09:00:00+08:00", "window-2",
                [{"lot_id": "sale-a", "quantity_kg": "10"}],
                "4.00", "999.00", settler_id="settler-1")

    def test_cannot_freeze_more_than_born(self) -> None:
        self.app.open_sale_window("w3", "2026-09-26T09:00:00+08:00",
                                  "window-3", "brand-1", "rule-1")
        with self.assertRaises(CommandError):
            self.app.freeze_premium(
                "f-bad", "2026-09-27T09:00:00+08:00", "window-3",
                [{"lot_id": "sale-a", "quantity_kg": "381"}],
                "4.00", "1524.00", settler_id="settler-1")

    # ---- 回执不重复结算 ----

    def test_receipt_cannot_be_settled_twice(self) -> None:
        self._post()
        # 同一批千克再开仓口冻结结算 → 已冻结/已结算超量拦截
        self.app.open_sale_window("w4", "2026-09-26T10:00:00+08:00",
                                  "window-4", "brand-1", "rule-1")
        with self.assertRaises(CommandError):
            self.app.freeze_premium(
                "f2", "2026-09-27T10:00:00+08:00", "window-4",
                [{"lot_id": "sale-a", "quantity_kg": "1"}],
                "4.00", "4.00", settler_id="settler-1")

    # ---- 复核差额 / 已付保留 / 待追回 ----

    def test_grade_revision_creates_delta_and_keeps_paid_fact(self) -> None:
        self._post()
        self.app.pay_settlement("pay-1", "2026-09-29T09:00:00+08:00", "ST-1",
                                [{"beneficiary_id": "farmer-a", "amount": "1974.00"}],
                                "2026-09-29T09:00:00+08:00", "settler-1")
        self.app.revise_grade("gr-1", "2026-09-30T09:00:00+08:00", "insp-1",
                              "GRADE_A", "inspector-1", "复检降级")
        self.app.revise_settlement("rs-1", "2026-09-30T10:00:00+08:00", "ST-1",
                                   "settler-1", "special 降级，按新版本计算差额")
        st = self.app.ledger.settlements["ST-1"]
        # 已付款事实仍在
        self.assertEqual(st["paid_amounts"]["farmer-a"], Decimal("1974.00"))
        # 新应得下降
        self.assertLess(self.line("farmer-a"), Decimal("1974.00"))
        # 负差额计入待追回
        overpaid = self.app.pending_work()["overpaid_awaiting_recovery"]
        self.assertTrue(any(o["beneficiary_id"] == "farmer-a" for o in overpaid))

    def test_revision_requires_reason(self) -> None:
        self._post()
        with self.assertRaises(CommandError):
            self.app.revise_settlement("rs-x", "2026-09-30T10:00:00+08:00", "ST-1",
                                       "settler-1", "  ")

    # ---- 品种纠错 ----

    def test_cultivar_correction_flows_downstream(self) -> None:
        self._post()
        self.app.register_cultivar("cv-v2", "2026-10-01T08:00:00+08:00", "YUNKA-1",
                                   "v2", "breeder-1", "云咖1号v2")
        self.app.correct_cultivar("cc-1", "2026-10-01T09:00:00+08:00", "buy-1",
                                  "YUNKA-1", "v2", "breeder-1", "底账登记错误")
        self.assertEqual(
            self.app.ledger.genealogy.effective_cultivar("sale-special"),
            ("YUNKA-1", "v2"),
        )
        # 已结算仓口进入待重算
        self.assertIn("window-1",
                      self.app.pending_work()["windows_awaiting_revision"])

    def test_mixed_cultivar_blocks_settlement(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        app2 = build_app(str(Path(tmp.name) / "e2.jsonl"))
        from tests.fixtures import seed_parties_and_goods
        seed_parties_and_goods(app2)
        T = "2026-09-{d:02d}T09:00:00+08:00"
        # 登记第二品种；两张地块交付后把其中一张收购批品种纠错为异源，再混批
        app2.register_cultivar("cv-other", T.format(d=8), "YUNKA-2", "v1",
                               "breeder-1", "云咖2号")
        app2.deliver_cherry("d-a", T.format(d=20), "R-A", "plot-a", "farmer-a",
                            "buy-A", "100", T.format(d=20))
        app2.deliver_cherry("d-b", T.format(d=20), "R-B", "plot-b", "farmer-b",
                            "buy-B", "100", T.format(d=20))
        app2.correct_cultivar("cc-b", T.format(d=20), "buy-B", "YUNKA-2", "v1",
                              "breeder-1", "B 批底账品种应为云咖2号")
        app2.merge_lots("m-mix", T.format(d=21),
                        [{"lot_id": "buy-A", "quantity_kg": "100"},
                         {"lot_id": "buy-B", "quantity_kg": "100"}], "mix")
        self.assertEqual(app2.ledger.genealogy.effective_cultivar("mix"),
                         ("MIXED", "MIXED"))
        app2.grade_lot("g-mix", T.format(d=24), "insp-m", "mix", "SPECIAL",
                       "inspector-1")
        app2.publish_rule("r-mix", T.format(d=25), "rule-mix", 1,
                          shares={"FARMER": "1"},
                          grade_rates={"SPECIAL": "1", "GRADE_A": "1", "REJECT": "0"},
                          settler_id="settler-1")
        app2.open_sale_window("w-mix", T.format(d=26), "window-mix", "brand-1",
                              "rule-mix")
        with self.assertRaises(CommandError):
            app2.freeze_premium("f-mix", T.format(d=27), "window-mix",
                                [{"lot_id": "mix", "quantity_kg": "200"}],
                                "4.00", "800.00", settler_id="settler-1")
        tmp.cleanup()

    # ---- 人工调整 ----

    def test_adjustment_requires_reason(self) -> None:
        self._post()
        with self.assertRaises(CommandError):
            self.app.adjust_settlement(
                "adj-x", "2026-10-01T09:00:00+08:00", "ST-1",
                [{"beneficiary_id": "farmer-b", "delta": "1.00", "reason": ""}],
                "settler-1")

    def test_adjustment_cannot_exceed_frozen(self) -> None:
        self._post()
        with self.assertRaises(CommandError):
            self.app.adjust_settlement(
                "adj-x", "2026-10-01T09:00:00+08:00", "ST-1",
                [{"beneficiary_id": "farmer-b", "delta": "999999.00",
                  "reason": "超额测试"}],
                "settler-1")

    def test_valid_adjustment_moves_reserve(self) -> None:
        self._post()
        before = self.line("farmer-b")
        reserve_before = self.line("RESERVE")
        self.app.adjust_settlement(
            "adj-1", "2026-10-01T09:00:00+08:00", "ST-1",
            [{"beneficiary_id": "farmer-b", "delta": "20.00",
              "reason": "绿色激励"},
             {"beneficiary_id": "RESERVE", "delta": "-20.00",
              "reason": "从代管结余列支"}],
            "settler-1")
        self.assertEqual(self.line("farmer-b"), before + Decimal("20.00"))
        self.assertEqual(self.line("RESERVE"), reserve_before - Decimal("20.00"))

    # ---- 争议冻结付款 ----

    def test_open_dispute_halts_payment(self) -> None:
        self._post()
        self.app.open_dispute("dp-1", "2026-10-02T09:00:00+08:00", "dispute-1",
                              "ST-1", "farmer-b", "扣减有异议")
        with self.assertRaises(CommandError):
            self.app.pay_settlement(
                "pay-x", "2026-10-02T10:00:00+08:00", "ST-1",
                [{"beneficiary_id": "farmer-b", "amount": "1.00"}],
                "2026-10-02T10:00:00+08:00", "settler-1")
        self.app.resolve_dispute("dpr-1", "2026-10-03T09:00:00+08:00", "dispute-1",
                                 "REJECTED", "按规则驳回", "settler-1")
        self.app.pay_settlement(
            "pay-2", "2026-10-03T10:00:00+08:00", "ST-1",
            [{"beneficiary_id": "farmer-b", "amount": "1.00"}],
            "2026-10-03T10:00:00+08:00", "settler-1")

    def test_cannot_pay_more_than_due(self) -> None:
        self._post()
        with self.assertRaises(CommandError):
            self.app.pay_settlement(
                "pay-x", "2026-09-29T09:00:00+08:00", "ST-1",
                [{"beneficiary_id": "farmer-a", "amount": "1974.01"}],
                "2026-09-29T09:00:00+08:00", "settler-1")

    # ---- 资格变更 ----

    def test_membership_change_moves_coop_share_in_revision(self) -> None:
        self._post()
        self.app.change_membership(
            "mc-1", "2026-10-01T09:00:00+08:00", "farmer-b", None,
            "2026-10-01T09:00:00+08:00", "退社")
        self.assertIn("window-1",
                      self.app.pending_work()["windows_awaiting_revision"])
        self.app.revise_settlement(
            "rs-mc", "2026-10-01T10:00:00+08:00", "ST-1", "settler-1",
            "farmer-b 退社，合作社份额按新资格重算")
        coop_new = self.line("coop-1")
        # B 的合作社份额取消，A 的仍在 → 合作社应得下降
        self.assertLess(coop_new, Decimal("366.60"))

    # ---- 农户隔离视图 ----

    def test_farmer_view_is_scoped(self) -> None:
        self._post()
        view_a = self.app.farmer_view("farmer-a")
        receipts_a = {r["receipt_no"] for r in view_a["receipts"]}
        self.assertEqual(receipts_a, {"R-001"})
        mine = view_a["settlements"][0]
        self.assertEqual(set(mine["my_receipts"]), {"R-001"})
        view_b = self.app.farmer_view("farmer-b")
        self.assertEqual({r["receipt_no"] for r in view_b["receipts"]}, {"R-002"})

    # ---- 中断恢复 ----

    def test_recovery_continues_pending_items(self) -> None:
        self._post()
        self.app.open_dispute("dp-1", "2026-10-02T09:00:00+08:00", "dispute-1",
                              "ST-1", "farmer-b", "异议")
        before = self.app.pending_work()
        app2 = build_app(str(Path(self.tmp.name) / "events.jsonl"))
        after = app2.pending_work()
        self.assertEqual(before, after)
        # 恢复后可以继续处理待裁决项
        app2.resolve_dispute("dpr-1", "2026-10-03T09:00:00+08:00", "dispute-1",
                             "UPHELD", "支持部分诉求", "settler-1")


if __name__ == "__main__":
    unittest.main()
