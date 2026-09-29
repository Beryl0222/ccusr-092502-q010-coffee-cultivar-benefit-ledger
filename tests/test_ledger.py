"""领域内核不变量测试。覆盖 contracts/domain.json 中登记的全部 invariants。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ledger import (
    ALLIANCE_ADMIN, FARMER, INSPECTOR, LICENSOR, NURSERY, PROCESSOR,
    SETTLEMENT_CLERK, DomainError, Ledger, scale_components,
)
from src.store import ConcurrencyError, DuplicateEventError, EventStore

ROOT = Path(__file__).resolve().parents[1]

T_LICENSE_FROM = "2026-01-01T00:00:00+08:00"
T_LICENSE_TO = "2027-01-01T00:00:00+08:00"
T_PLANT = "2026-03-01T09:00:00+08:00"
T_HARVEST = "2026-08-10T10:00:00+08:00"
T_DELIVER = "2026-08-11T08:00:00+08:00"
T_GRADE = "2026-08-12T08:00:00+08:00"
T_FREEZE = "2026-09-01T09:00:00+08:00"
T_SETTLE = "2026-09-05T09:00:00+08:00"

LICENSOR_A = {"actor_id": "u-zhang", "roles": [LICENSOR]}
INSPECTOR_LI = {"actor_id": "u-li", "roles": [INSPECTOR]}
CLERK_WANG = {"actor_id": "u-wang", "roles": [SETTLEMENT_CLERK]}
ADMIN_CHEN = {"actor_id": "u-chen", "roles": [ALLIANCE_ADMIN]}
NURSERY_RUN = {"actor_id": "u-nursery-01", "roles": [NURSERY]}
FARMER_JIA = {"actor_id": "f-jia", "roles": [FARMER]}
FARMER_YI = {"actor_id": "f-yi", "roles": [FARMER]}
PROC_Q = {"actor_id": "u-proc-q", "roles": [PROCESSOR]}


def build_world(*, yi_green: bool = True, freeze: bool = True,
                graded: bool = True) -> Ledger:
    """构造标准双农户场景：甲 plot-a、乙 plot-b，拆批后混批入同一仓口。"""
    lg = Ledger()
    lg.register_cultivar_version(LICENSOR_A, "cv-y1", "云咖1号", 1,
                                 occurred_at="2026-01-05T09:00:00+08:00")
    lg.register_cultivar_version(LICENSOR_A, "cv-y2", "云咖1号", 2, supersedes="cv-y1",
                                 occurred_at="2026-01-05T09:05:00+08:00")
    lg.grant_license(LICENSOR_A, "lic-1", "cv-y1", "普洱片区",
                     T_LICENSE_FROM, T_LICENSE_TO, 10_000,
                     occurred_at="2026-01-10T09:00:00+08:00")
    lg.create_nursery_batch(NURSERY_RUN, "nb-1", "cv-y1", "lic-1", 5_000,
                            occurred_at="2026-02-01T09:00:00+08:00")
    lg.transfer_seedlings(NURSERY_RUN, "tr-1", "nb-1", "f-jia", 2_000,
                          occurred_at="2026-02-10T09:00:00+08:00")
    lg.transfer_seedlings(NURSERY_RUN, "tr-2", "nb-1", "f-yi", 1_000,
                          occurred_at="2026-02-10T09:30:00+08:00")
    lg.change_membership(ADMIN_CHEN, "coop-1", "ACTIVE", reason="成立入会")
    lg.plant_plot(FARMER_JIA, "plot-a", "f-jia", "cv-y1", 2_000,
                  cooperative_id="coop-1", license_id="lic-1", occurred_at=T_PLANT)
    lg.plant_plot(FARMER_YI, "plot-b", "f-yi", "cv-y1", 1_000,
                  cooperative_id="coop-1", license_id="lic-1", occurred_at=T_PLANT)

    lg.record_farming(FARMER_JIA, "rec-a", "plot-a", "绿色防控+有机肥", T_HARVEST)
    lg.verify_green_compliance(INSPECTOR_LI, "ver-a", "rec-a", "plot-a", True)
    lg.record_farming(FARMER_YI, "rec-b", "plot-b", "有机肥", T_HARVEST)
    if yi_green:
        lg.verify_green_compliance(INSPECTOR_LI, "ver-b", "rec-b", "plot-b", True)
    else:
        lg.verify_green_compliance(INSPECTOR_LI, "ver-b-fail", "rec-b", "plot-b",
                                   False, note="用药记录断链")
        lg.open_dispute(ADMIN_CHEN, "disp-green-b", "plot-b",
                        "绿色技术履行断链", detail=" ver-b-fail")

    # 甲 100kg 拆 40/60；乙 60kg；甲的 60kg 与乙混批
    lg.harvest(FARMER_JIA, "lot-a", "plot-a", 100_000, "rcpt-1", occurred_at=T_HARVEST)
    lg.harvest(FARMER_YI, "lot-b", "plot-b", 60_000, "rcpt-2",
               occurred_at=T_HARVEST)
    lg.split_lot(FARMER_JIA, "lot-a", [("lot-a1", 40_000), ("lot-a2", 60_000)])
    lg.merge_lots(FARMER_JIA, "lot-m", ["lot-a2", "lot-b"])
    lg.deliver(PROC_Q, "d1", "lot-a1", "u-proc-q", "sb-1", 40_000,
               occurred_at=T_DELIVER)
    lg.deliver(PROC_Q, "d2", "lot-m", "u-proc-q", "sb-1", 120_000,
               occurred_at=T_DELIVER)
    if graded:
        lg.grade(INSPECTOR_LI, "gr-d1", "d1", "AA", 1.2, occurred_at=T_GRADE)
        lg.grade(INSPECTOR_LI, "gr-d2", "d2", "A", 1.0, occurred_at=T_GRADE)
    if freeze:
        lg.freeze_premium(CLERK_WANG, "sb-1", 1_000_000, "品牌:云咖优品",
                          occurred_at=T_FREEZE)
        lg.set_rules(CLERK_WANG, "rules-1", 0.7, 0.2, 0.1)
    return lg


def settle(lg: Ledger, *, proposal="prop-1", settlement="st-1",
           at=T_SETTLE) -> dict:
    lg.propose_allocation(CLERK_WANG, proposal, "sb-1")
    return lg.post_settlement(CLERK_WANG, settlement, proposal, occurred_at=at)


class ConservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lg = build_world()

    def test_split_preserves_plot_components(self) -> None:
        st = self.lg.state
        self.assertEqual(st.cherry_nodes["lot-a1"]["components"], {"plot-a": 40_000})
        self.assertEqual(st.cherry_nodes["lot-a2"]["components"], {"plot-a": 60_000})
        # 父批被全部拆出后余量为 0，节点保留作为谱系锚点
        self.assertEqual(st.cherry_nodes["lot-a"]["weight_g"], 0)

    def test_merge_components_equal_sum_of_inputs(self) -> None:
        comp = self.lg.state.cherry_nodes["lot-m"]["components"]
        self.assertEqual(comp, {"plot-a": 60_000, "plot-b": 60_000})

    def test_delivery_weight_equals_lot_weight(self) -> None:
        for d in ("d1", "d2"):
            node = self.lg.state.cherry_nodes[self.lg.state.deliveries[d]["cherry_lot_id"]]
            self.assertEqual(self.lg.state.deliveries[d]["weight_g"], node["weight_g"])
        total_in = sum(d["weight_g"] for d in self.lg.state.deliveries.values())
        self.assertEqual(total_in, 160_000)

    def test_split_over_parent_weight_rejected(self) -> None:
        self.lg.harvest(FARMER_JIA, "lot-x", "plot-a", 10_000, "rcpt-x")
        with self.assertRaisesRegex(DomainError, "数量不守恒"):
            self.lg.split_lot(FARMER_JIA, "lot-x", [("x1", 6_000), ("x2", 6_000)])

    def test_consumed_lot_cannot_merge_or_deliver_again(self) -> None:
        with self.assertRaisesRegex(DomainError, "已消耗"):
            self.lg.merge_lots(FARMER_YI, "lot-m2", ["lot-b"])
        with self.assertRaisesRegex(DomainError, "已消耗"):
            self.lg.deliver(PROC_Q, "d3", "lot-a1", "u-proc-q", "sb-1", 40_000)

    def test_deliver_weight_mismatch_rejected(self) -> None:
        self.lg.harvest(FARMER_YI, "lot-y", "plot-b", 5_000, "rcpt-y")
        with self.assertRaisesRegex(DomainError, "数量不守恒"):
            self.lg.deliver(PROC_Q, "d-y", "lot-y", "u-proc-q", "sb-2", 5_001)

    def test_largest_remainder_split_conservation(self) -> None:
        parent = {"a": 333, "b": 333, "c": 334}
        child = scale_components(parent, 100, 1_000)
        self.assertEqual(sum(child.values()), 100)                 # 子批分尽
        rest = scale_components(parent, 900, 1_000)
        for k in parent:
            self.assertEqual(child[k] + rest[k], parent[k])       # 子+余=父
        # 混批后多地块父批再拆：按比例守恒
        mixed = {"plot-a": 60_000, "plot-b": 60_000}
        self.assertEqual(scale_components(mixed, 40_000, 120_000),
                         {"plot-a": 20_000, "plot-b": 20_000})

    def test_seedling_quantity_chain_conservation(self) -> None:
        lg = Ledger()
        lg.register_cultivar_version(LICENSOR_A, "cv-1", "v", 1)
        lg.grant_license(LICENSOR_A, "lic", "cv-1", "片区", T_LICENSE_FROM,
                         T_LICENSE_TO, 1_000)
        # 累计繁育超授权株数
        lg.create_nursery_batch(NURSERY_RUN, "nb", "cv-1", "lic", 1_000)
        with self.assertRaisesRegex(DomainError, "授权株数"):
            lg.create_nursery_batch(NURSERY_RUN, "nb2", "cv-1", "lic", 1)
        # 出苗超批次产量
        with self.assertRaisesRegex(DomainError, "批次产量"):
            lg.transfer_seedlings(NURSERY_RUN, "tr", "nb", "f-jia", 1_001)
        lg.transfer_seedlings(NURSERY_RUN, "tr", "nb", "f-jia", 100)
        # 实种超持有
        with self.assertRaisesRegex(DomainError, "数量不守恒"):
            lg.plant_plot(FARMER_JIA, "plot", "f-jia", "cv-1", 101)
        lg.plant_plot(FARMER_JIA, "plot", "f-jia", "cv-1", 100)
        self.assertEqual(lg.state.seedlings_held["f-jia"], 0)


class ReceiptIdempotencyTest(unittest.TestCase):
    def test_duplicate_receipt_returns_first_event(self) -> None:
        lg = build_world()
        before = len(lg.store.events)
        first = next(e for e in lg.store.events
                     if e["event_type"] == "CHERRY_HARVESTED"
                     and e["payload"]["receipt_id"] == "rcpt-1")
        again = lg.harvest(FARMER_JIA, "lot-a-dup", "plot-a", 999_999, "rcpt-1",
                           occurred_at=T_HARVEST)
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual(len(lg.store.events), before)  # 不新增事实

    def test_idempotency_key_enforced_by_store(self) -> None:
        store = EventStore()
        evt = {"event_id": "e1", "event_type": "X", "occurred_at": T_HARVEST,
               "aggregate_id": "a1", "version": 1, "payload": {},
               "idempotency_key": "k1"}
        store.append(evt)
        # 换聚合与版本，相同幂等键仍须拒绝
        with self.assertRaises(DuplicateEventError):
            store.append({**evt, "event_id": "e2", "aggregate_id": "a2"})

    def test_aggregate_version_must_be_gapless(self) -> None:
        store = EventStore()
        base = {"event_type": "X", "occurred_at": T_HARVEST, "payload": {}}
        store.append({"event_id": "e1", "aggregate_id": "a", "version": 1, **base})
        with self.assertRaises(ConcurrencyError):
            store.append({"event_id": "e2", "aggregate_id": "a", "version": 3, **base})


class CorrectionAndAdjustmentTest(unittest.TestCase):
    def test_green_chain_restored_triggers_makeup_adjustment(self) -> None:
        lg = build_world(yi_green=False)
        settle(lg)
        # 首轮只有甲合格
        self.assertEqual(lg.state.farmer_settled["f-jia"], 450_000)
        self.assertEqual(lg.state.farmer_settled["f-yi"], 0)
        first = lg.state.settlements["st-1"]
        first_amount = first["premium_total_fen"]

        # 乙补齐绿色履行、争议裁决合格 → 按新版本重算差额
        lg.verify_green_compliance(INSPECTOR_LI, "ver-b-pass", "rec-b", "plot-b",
                                   True, note="补充用药记录")
        lg.resolve_dispute(ADMIN_CHEN, "disp-green-b", "plot-b",
                           "断链已补齐，认定合格", eligible=True)
        lg.post_adjustment(CLERK_WANG, "adj-1", "sb-1",
                           "绿色断链恢复后按新版本重算")
        self.assertEqual(lg.state.farmer_settled["f-yi"], 250_000)
        # 甲的加权份额恰好不变；冻结池被用满但不超出
        self.assertEqual(lg.state.farmer_settled["f-jia"], 450_000)
        self.assertLessEqual(lg.state.allocated["sb-1"], lg.state.frozen["sb-1"])
        # 原结算事实保持原样，没有被删除或改写
        self.assertIs(lg.state.settlements["st-1"], first)
        self.assertEqual(first["premium_total_fen"], first_amount)
        self.assertEqual(first["status"], "POSTED")

    def test_grade_review_changes_weighted_share(self) -> None:
        lg = build_world()
        settle(lg)
        # 同值复核不产生差额
        lg.review_grade(INSPECTOR_LI, "gr-d2-r0", "d2", "A", 1.0,
                        note="复核维持原判", occurred_at="2026-09-06T08:00:00+08:00")
        with self.assertRaisesRegex(DomainError, "无差额"):
            lg.post_adjustment(CLERK_WANG, "adj-noop", "sb-1", "无变化复核")
        # d2（混批：甲60kg+乙60kg）复核升等，权重 1.0 → 1.1
        jia_before = lg.state.farmer_settled["f-jia"]
        yi_before = lg.state.farmer_settled["f-yi"]
        lg.review_grade(INSPECTOR_LI, "gr-d2-r", "d2", "A+", 1.1,
                        note="杯测复核", occurred_at="2026-09-07T08:00:00+08:00")
        lg.post_adjustment(CLERK_WANG, "adj-2", "sb-1", "检验复核后按新等级权重重算")
        self.assertNotEqual(
            (lg.state.farmer_settled["f-jia"], lg.state.farmer_settled["f-yi"]),
            (jia_before, yi_before))
        grade_events = [e for e in lg.store.events
                        if e["payload"].get("delivery_id") == "d2"]
        self.assertEqual([e["event_type"] for e in grade_events],
                         ["LOT_GRADED", "GRADE_REVIEWED", "GRADE_REVIEWED"])
        self.assertLessEqual(lg.state.allocated["sb-1"], lg.state.frozen["sb-1"])

    def test_cultivar_correction_to_unlicensed_version_claws_back(self) -> None:
        lg = build_world()
        settle(lg)
        # 甲地块品种被纠错为 cv-y2，但 lic-1 只覆盖 cv-y1 → 甲丧失合格性
        lg.correct_cultivar(ADMIN_CHEN, "corr-a", "plot-a", "cv-y2",
                            "种苗标签核查发现品种登记错误")
        adj = lg.post_adjustment(CLERK_WANG, "adj-3", "sb-1",
                                 "品种纠错：plot-a 不在授权范围")
        self.assertEqual(lg.state.farmer_settled["f-jia"], 0)
        self.assertTrue(any(l["delta_fen"] < 0 for l in adj["payload"]["lines"]))
        # 旧品种事实仍可追溯：实种事件与纠错事件都在
        types = [e["event_type"] for e in lg.store.events]
        self.assertIn("PLOT_PLANTED", types)
        self.assertIn("CULTIVAR_CORRECTED", types)

    def test_membership_suspended_then_restated_recalculates(self) -> None:
        lg = build_world()
        settle(lg)
        self.assertGreater(lg.state.farmer_settled["f-jia"], 0)
        lg.change_membership(ADMIN_CHEN, "coop-1", "SUSPENDED", reason="年度审查暂停")
        lg.post_adjustment(CLERK_WANG, "adj-4", "sb-1", "合作社资格暂停，合格量冲回")
        self.assertEqual(lg.state.farmer_settled["f-jia"], 0)
        self.assertEqual(lg.state.farmer_settled["f-yi"], 0)
        # 恢复资格 → 再次补发
        lg.change_membership(ADMIN_CHEN, "coop-1", "ACTIVE", reason="审查通过恢复")
        lg.post_adjustment(CLERK_WANG, "adj-5", "sb-1", "合作社资格恢复，补发溢价")
        self.assertEqual(lg.state.farmer_settled["f-jia"], 450_000)
        self.assertEqual(lg.state.farmer_settled["f-yi"], 250_000)

    def test_negative_adjustment_against_paid_settlement_rejected(self) -> None:
        lg = build_world()
        settle(lg)
        lg.pay_settlement(CLERK_WANG, "st-1", "pay-001")
        lg.change_membership(ADMIN_CHEN, "coop-1", "SUSPENDED", reason="事后暂停")
        with self.assertRaisesRegex(DomainError, "已付款事实不能被冲销"):
            lg.post_adjustment(CLERK_WANG, "adj-x", "sb-1", "试图冲销已付款")
        # 已付款事实不动
        self.assertEqual(lg.state.settlements["st-1"]["status"], "PAID")

    def test_proposal_cannot_post_twice(self) -> None:
        lg = build_world()
        settle(lg)
        with self.assertRaisesRegex(DomainError, "提案已过账"):
            lg.post_settlement(CLERK_WANG, "st-2", "prop-1")

    def test_payment_cannot_register_twice(self) -> None:
        lg = build_world()
        settle(lg)
        lg.pay_settlement(CLERK_WANG, "st-1", "pay-001")
        with self.assertRaisesRegex(DomainError, "不能重复登记付款"):
            lg.pay_settlement(CLERK_WANG, "st-1", "pay-002")

    def test_events_are_append_only(self) -> None:
        lg = build_world()
        settle(lg)
        n = len(lg.store.events)
        lg.correct_cultivar(ADMIN_CHEN, "corr-a", "plot-a", "cv-y2", "纠错")
        # 纠错只追加事件，PLOT_PLANTED 原事件载荷不变
        planted = [e for e in lg.store.events
                   if e["event_type"] == "PLOT_PLANTED" and e["aggregate_id"] == "plot-a"][0]
        self.assertEqual(planted["payload"]["cultivar_version_id"], "cv-y1")
        self.assertEqual(len(lg.store.events), n + 1)


class SeparationOfDutiesTest(unittest.TestCase):
    def test_role_guards_reject_cross_signing(self) -> None:
        lg = build_world()
        # 检验员不能签授权
        with self.assertRaisesRegex(DomainError, "LICENSOR"):
            lg.grant_license(INSPECTOR_LI, "lic-x", "cv-y1", "片区",
                             T_LICENSE_FROM, T_LICENSE_TO, 1)
        # 授权人不能定级
        with self.assertRaisesRegex(DomainError, "INSPECTOR"):
            lg.grade(LICENSOR_A, "gr-x", "d1", "AA", 1.0)
        # 检验员不能过账结算
        with self.assertRaisesRegex(DomainError, "SETTLEMENT_CLERK"):
            lg.propose_allocation(INSPECTOR_LI, "prop-x", "sb-1")

    def test_same_natural_person_cannot_hold_two_sensitive_roles(self) -> None:
        lg = build_world(freeze=False)
        # 该 actor 已以检验员身份定级
        lg.grade({"actor_id": "u-same", "roles": [INSPECTOR]}, "gr-s1", "d1", "AA", 1.2)
        # 再试图以结算人身份操作 → 拒绝代签
        with self.assertRaisesRegex(DomainError, "不得再以"):
            lg.freeze_premium({"actor_id": "u-same", "roles": [SETTLEMENT_CLERK]},
                              "sb-1", 1, "品牌")
        # 反向也一样：先结算后检验
        lg.freeze_premium(CLERK_WANG, "sb-1", 1_000_000, "品牌")
        with self.assertRaisesRegex(DomainError, "不得再以"):
            lg.grade({"actor_id": "u-wang", "roles": [INSPECTOR]}, "gr-w", "d1", "AA", 1.2)

    def test_sensitive_events_require_actor(self) -> None:
        lg = build_world()
        with self.assertRaises(DomainError):
            lg._append("SETTLEMENT_POSTED", "s-x", {"lines": []}, None)

    def test_role_history_rebuilds_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            lg = Ledger(EventStore(path, allowed_events=Ledger.EVENTS))
            lg.register_cultivar_version(LICENSOR_A, "cv-z", "z", 1)
            lg.grant_license(LICENSOR_A, "lic-z", "cv-z", "片区", T_LICENSE_FROM,
                             T_LICENSE_TO, 10)
            # 重建进程
            lg2 = Ledger(EventStore(path, allowed_events=Ledger.EVENTS))
            self.assertIn(LICENSOR, lg2.state.used_roles["u-zhang"])
            with self.assertRaisesRegex(DomainError, "不得再以"):
                lg2.grade({"actor_id": "u-zhang", "roles": [INSPECTOR]},
                          "gr-z", "d1", "AA", 1.0)


class ManualAdjustmentTest(unittest.TestCase):
    def test_reason_required(self) -> None:
        lg = build_world()
        settle(lg)
        with self.assertRaisesRegex(DomainError, "理由"):
            lg.manual_adjust(CLERK_WANG, "ma-1", "sb-1",
                             [{"farmer_id": "f-jia", "delta_fen": 100}], "   ")

    def test_cannot_exceed_frozen_pool(self) -> None:
        lg = build_world()
        settle(lg)
        remaining = lg.state.frozen["sb-1"] - lg.state.allocated["sb-1"]
        with self.assertRaisesRegex(DomainError, "冻结销售收益"):
            lg.manual_adjust(CLERK_WANG, "ma-1", "sb-1",
                             [{"farmer_id": "f-yi", "delta_fen": remaining + 1}],
                             "超额人工补偿")

    def test_valid_manual_adjustment_conserves_pool(self) -> None:
        # 乙绿色断链 → 首轮冻结池只用掉合格部分，留有余额
        lg = build_world(yi_green=False)
        settle(lg)
        frozen = lg.state.frozen["sb-1"]
        self.assertLess(lg.state.allocated["sb-1"], frozen)
        remaining = frozen - lg.state.allocated["sb-1"]
        lg.manual_adjust(CLERK_WANG, "ma-1", "sb-1",
                         [{"farmer_id": "f-yi", "delta_fen": remaining}],
                         "合作社大会决议的帮扶补价")
        self.assertEqual(lg.state.allocated["sb-1"], frozen)
        reason = [e for e in lg.store.events
                  if e["event_type"] == "MANUAL_SHARE_ADJUSTED"][0]["payload"]["reason"]
        self.assertEqual(reason, "合作社大会决议的帮扶补价")


class ViewAndTraceTest(unittest.TestCase):
    def test_farmer_view_is_isolated(self) -> None:
        lg = build_world()
        settle(lg)
        view = json.dumps(lg.farmer_view("f-jia"), ensure_ascii=False)
        self.assertNotIn("f-yi", view)
        self.assertNotIn("plot-b", view)
        self.assertNotIn("lot-b", view)
        # 甲能看到自己的谱系依据
        self.assertIn("plot-a", view)
        self.assertIn("st-1", view)
        # 乙的视图同理
        view_yi = json.dumps(lg.farmer_view("f-yi"), ensure_ascii=False)
        self.assertNotIn("f-jia", view_yi)

    def test_premium_trace_walks_full_lineage(self) -> None:
        lg = build_world()
        settle(lg)
        lg.pay_settlement(CLERK_WANG, "st-1", "pay-001")
        trace = lg.premium_trace("sb-1", farmer_id="f-jia")
        edge_pairs = {(e["from"], e["to"]) for e in trace["lineage"]}
        # 品牌仓口 → 交付 → 混批/拆批 → 甲的采收批次，全链可达
        self.assertIn(("d1", "sb-1"), edge_pairs)
        self.assertIn(("lot-a1", "d1"), edge_pairs)
        self.assertIn(("lot-a", "lot-a2"), edge_pairs)
        self.assertIn(("lot-a2", "lot-m"), edge_pairs)
        self.assertIn(("lot-m", "d2"), edge_pairs)
        # 乙的成分出现在依据中，但 received 只含甲
        comps = {(c["plot_id"], c["farmer_id"])
                 for b in trace["deliveries"] for c in b["components"]}
        self.assertIn(("plot-b", "f-yi"), comps)
        self.assertTrue(trace["conservation_ok"])
        self.assertEqual({r["farmer_id"] for r in trace["received"]}, {"f-jia"})
        self.assertTrue(all(r["status"] == "PAID" for r in trace["received"]))

    def test_ineligible_components_explained_in_basis(self) -> None:
        lg = build_world(yi_green=False)
        lg.propose_allocation(CLERK_WANG, "prop-1", "sb-1")
        proposal = lg.state.proposals["prop-1"]
        yi_rows = [c for b in proposal["delivery_bases"] for c in b["components"]
                   if c["plot_id"] == "plot-b"]
        self.assertTrue(yi_rows)
        self.assertFalse(yi_rows[0]["eligible"])
        self.assertTrue(any("绿色" in r for r in yi_rows[0]["reasons"]))
        # 依据内嵌检验版本、品种版本、绿色核验事件、谱系边
        basis = proposal["delivery_bases"][1]
        self.assertEqual(basis["grade_event"]["grade"], "A")
        self.assertTrue(basis["lineage"])
        self.assertTrue(yi_rows[0]["green_records"] == [])


class RecoveryTest(unittest.TestCase):
    def test_replay_rebuilds_pending_and_allows_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            store = EventStore(path, allowed_events=Ledger.EVENTS)
            lg = Ledger(store)
            lg.register_cultivar_version(LICENSOR_A, "cv-y1", "云咖1号", 1)
            lg.grant_license(LICENSOR_A, "lic-1", "cv-y1", "片区", T_LICENSE_FROM,
                             T_LICENSE_TO, 10_000)
            lg.create_nursery_batch(NURSERY_RUN, "nb-1", "cv-y1", "lic-1", 5_000)
            lg.transfer_seedlings(NURSERY_RUN, "tr-1", "nb-1", "f-jia", 2_000)
            lg.change_membership(ADMIN_CHEN, "coop-1", "ACTIVE")
            lg.plant_plot(FARMER_JIA, "plot-a", "f-jia", "cv-y1", 2_000,
                          cooperative_id="coop-1", license_id="lic-1")
            lg.record_farming(FARMER_JIA, "rec-a", "plot-a", "绿色防控", T_HARVEST)
            lg.verify_green_compliance(INSPECTOR_LI, "ver-a", "rec-a", "plot-a", True)
            lg.harvest(FARMER_JIA, "lot-a", "plot-a", 100_000, "rcpt-1",
                       occurred_at=T_HARVEST)
            lg.deliver(PROC_Q, "d1", "lot-a", "u-proc-q", "sb-1", 100_000,
                       occurred_at=T_DELIVER)
            lg.grade(INSPECTOR_LI, "gr-d1", "d1", "AA", 1.2, occurred_at=T_GRADE)
            lg.freeze_premium(CLERK_WANG, "sb-1", 500_000, "品牌", occurred_at=T_FREEZE)
            lg.set_rules(CLERK_WANG, "rules-1", 0.7, 0.2, 0.1)
            lg.propose_allocation(CLERK_WANG, "prop-1", "sb-1")
            lg.post_settlement(CLERK_WANG, "st-1", "prop-1")  # 过账后、付款前“中断”
            del lg, store

            # 新进程从 JSONL 重放
            lg2 = Ledger(EventStore(path, allowed_events=Ledger.EVENTS))
            pending = lg2.pending_work()
            self.assertIn("st-1", pending["unpaid_posted_settlements"])
            self.assertEqual(pending["frozen_status"][0]["remaining_fen"], 0)
            # 已过账提案不会重复过账；付款可继续
            with self.assertRaisesRegex(DomainError, "提案已过账"):
                lg2.post_settlement(CLERK_WANG, "st-dup", "prop-1")
            lg2.pay_settlement(CLERK_WANG, "st-1", "pay-after-restart")
            self.assertEqual(lg2.state.settlements["st-1"]["status"], "PAID")
            # 回执重放在新进程仍然幂等
            n = len(lg2.store.events)
            lg2.harvest(FARMER_JIA, "lot-dup", "plot-a", 1, "rcpt-1")
            self.assertEqual(len(lg2.store.events), n)

    def test_open_disputes_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            lg = Ledger(EventStore(path, allowed_events=Ledger.EVENTS))
            lg.register_cultivar_version(LICENSOR_A, "cv-y1", "v", 1)
            lg.grant_license(LICENSOR_A, "lic-1", "cv-y1", "片区", T_LICENSE_FROM,
                             T_LICENSE_TO, 10_000)
            lg.create_nursery_batch(NURSERY_RUN, "nb", "cv-y1", "lic-1", 100)
            lg.transfer_seedlings(NURSERY_RUN, "tr", "nb", "f-jia", 50)
            lg.plant_plot(FARMER_JIA, "plot-a", "f-jia", "cv-y1", 50, license_id="lic-1")
            lg.harvest(FARMER_JIA, "lot-a", "plot-a", 1_000, "r1", occurred_at=T_HARVEST)
            lg.open_dispute(ADMIN_CHEN, "disp-1", "lot-a", "品种标识待核")
            lg2 = Ledger(EventStore(path, allowed_events=Ledger.EVENTS))
            self.assertEqual(lg2.pending_work()["open_disputes"].get("lot-a"),
                             ["disp-1"])


class ContractSyncTest(unittest.TestCase):
    def test_code_events_match_contract(self) -> None:
        contract = json.loads((ROOT / "contracts" / "domain.json").read_text("utf-8"))
        self.assertEqual(set(contract["events"]), set(Ledger.EVENTS))


if __name__ == "__main__":
    unittest.main()
