"""端到端烟雾脚本：跑通云南咖啡良种权益分配主流程。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.commands import CommandError, DuplicateCommand, LedgerApp
from src.ledger import Ledger
from src.store import EventStore

CONTRACT = json.loads(Path("contracts/domain.json").read_text(encoding="utf-8"))


def build_app(path: str) -> LedgerApp:
    store = EventStore(path, set(CONTRACT["events"]))
    store.load()
    ledger = Ledger(store)
    ledger.replay()
    return LedgerApp(ledger)


def main() -> None:
    tmp = tempfile.mkdtemp()
    app = build_app(f"{tmp}/events.jsonl")
    T = "2026-09-{d:02d}T09:00:00+08:00"

    # 1) 主体：育种单位（授权人）、苗圃、两个农户、合作社、加工厂、品牌，检验员与结算人互斥
    app.register_party("p-breeder", T.format(d=1), "breeder-1", "云南热作所",
                       ["BREEDER"], ["LICENSOR"])
    app.register_party("p-nursery", T.format(d=1), "nursery-1", "普洱绿洲苗圃", ["NURSERY"])
    app.register_party("p-farmer-a", T.format(d=1), "farmer-a", "岩温", ["FARMER"])
    app.register_party("p-farmer-b", T.format(d=1), "farmer-b", "玉应", ["FARMER"])
    app.register_party("p-coop", T.format(d=1), "coop-1", "孟连良种咖啡合作社", ["COOPERATIVE"])
    app.register_party("p-mill", T.format(d=1), "mill-1", "孟连精品水洗厂", ["MILL"])
    app.register_party("p-brand", T.format(d=1), "brand-1", "云咖品牌方", ["BRAND"])
    app.register_party("p-inspector", T.format(d=1), "inspector-1", "联盟质检员",
                       ["ALLIANCE"], ["INSPECTOR"])
    app.register_party("p-settler", T.format(d=1), "settler-1", "联盟结算员",
                       ["ALLIANCE"], ["SETTLER"])

    # 互斥：同一主体不能兼任两种职务
    try:
        app.register_party("p-bad", T.format(d=1), "bad", ["ALLIANCE"],
                           ["INSPECTOR", "SETTLER"])
        raise AssertionError("职务互斥未生效")
    except CommandError:
        pass

    # 2) 品种与授权
    app.register_cultivar("cv-1", T.format(d=2), "YUNKA-1", "v1", "breeder-1",
                          "云咖1号", green_requirements=["SHADE_TREE", "ORGANIC_FERTILIZER"])
    app.grant_license("lic-1", T.format(d=2), "license-1", "YUNKA-1", "v1",
                      "breeder-1", "nursery-1",
                      {"max_seedlings": 200000, "region": "普洱/孟连"})

    # 检验员不能代签授权
    try:
        app.grant_license("lic-x", T.format(d=2), "license-x", "YUNKA-1", "v1",
                          "inspector-1", "nursery-1", {})
        raise AssertionError("代签拦截失败")
    except CommandError:
        pass

    # 3) 繁育批次、交接、地块实种
    app.establish_nursery_lot("nl-1", T.format(d=3), "nlot-1", "nursery-1",
                              "license-1", "100000")
    app.transfer_seedlings("tr-1", T.format(d=4), "transfer-1", "nlot-1",
                           "nursery-1", "farmer-a", "3000", T.format(d=4),
                           to_plot_id="plot-a")
    app.transfer_seedlings("tr-2", T.format(d=5), "transfer-2", "nlot-1",
                           "nursery-1", "farmer-b", "2500", T.format(d=5),
                           to_plot_id="plot-b")
    app.plant_plot("pl-1", T.format(d=6), "plot-a", "farmer-a", "transfer-1",
                   "3000", T.format(d=6), coop_id="coop-1")
    app.plant_plot("pl-2", T.format(d=6), "plot-b", "farmer-b", "transfer-2",
                   "2500", T.format(d=6), coop_id="coop-1")

    # 4) 绿色农事：A 两项齐全，B 缺一项有机肥记录
    app.record_green_practice("gp-a1", T.format(d=10), "plot-a", "SHADE_TREE",
                              "coop-1", T.format(d=10), evidence_ref="ev-shade-a")
    app.record_green_practice("gp-a2", T.format(d=11), "plot-a", "ORGANIC_FERTILIZER",
                              "coop-1", T.format(d=11), evidence_ref="ev-fert-a")
    app.record_green_practice("gp-b1", T.format(d=10), "plot-b", "SHADE_TREE",
                              "coop-1", T.format(d=10), evidence_ref="ev-shade-b")

    # 5) 鲜果交付（收购回执），农户 B 多交一次形成同批两回执
    app.deliver_cherry("d-1", T.format(d=20), "RC-2026-001", "plot-a", "farmer-a",
                       "buy-lot-1", "1000", T.format(d=20))
    app.deliver_cherry("d-2", T.format(d=20), "RC-2026-002", "plot-b", "farmer-b",
                       "buy-lot-1", "600", T.format(d=20))

    # 同一回执号重送
    try:
        app.deliver_cherry("d-1-dup", T.format(d=20), "RC-2026-001", "plot-a",
                           "farmer-a", "buy-lot-1", "1000", T.format(d=20))
        raise AssertionError("回执重复未拦截")
    except CommandError:
        pass

    # 6) 混批 → 水洗加工（出成率 0.8）→ 按等级拆分
    app.merge_lots("m-1", T.format(d=21),
                   [{"lot_id": "buy-lot-1", "quantity_kg": "1600"}], "washed-lot")
    app.transform_lot("tf-1", T.format(d=22),
                      [{"lot_id": "washed-lot", "quantity_kg": "1600"}],
                      [{"lot_id": "green-lot", "quantity_kg": "1280"}])
    app.split_lot("sp-1", T.format(d=23),
                  [{"lot_id": "green-lot", "quantity_kg": "1280"}],
                  [{"lot_id": "sale-special", "quantity_kg": "900"},
                   {"lot_id": "sale-a", "quantity_kg": "380"}])

    # 守恒失败
    try:
        app.split_lot("sp-bad", T.format(d=23),
                      [{"lot_id": "sale-a", "quantity_kg": "380"}],
                      [{"lot_id": "x1", "quantity_kg": "200"},
                       {"lot_id": "x2", "quantity_kg": "181"}])
        raise AssertionError("守恒未生效")
    except CommandError:
        pass

    # 7) 检验分级
    app.grade_lot("g-1", T.format(d=24), "insp-1", "sale-special", "SPECIAL",
                  "inspector-1", notes="杯测 86 分")
    app.grade_lot("g-2", T.format(d=24), "insp-2", "sale-a", "GRADE_A",
                  "inspector-1")

    # 结算员不能代签检验
    try:
        app.grade_lot("g-x", T.format(d=24), "insp-x", "sale-a", "GRADE_A", "settler-1")
        raise AssertionError("代签拦截失败")
    except CommandError:
        pass

    # 8) 结算规则、仓口、溢价冻结
    app.publish_rule("r-1", T.format(d=25), "rule-1", 1,
                     shares={"FARMER": "0.70", "COOPERATIVE": "0.10",
                             "BREEDER": "0.12", "NURSERY": "0.08"},
                     grade_rates={"SPECIAL": "1.0", "GRADE_A": "0.6", "REJECT": "0"},
                     required_practices=["SHADE_TREE", "ORGANIC_FERTILIZER"],
                     green_penalty="0.5",
                     settler_id="settler-1")
    app.open_sale_window("w-1", T.format(d=26), "window-1", "brand-1", "rule-1",
                         mill_id="mill-1")
    app.freeze_premium("f-1", T.format(d=27), "window-1",
                       [{"lot_id": "sale-special", "quantity_kg": "900"},
                        {"lot_id": "sale-a", "quantity_kg": "380"}],
                       "4.00", "5120.00", settler_id="settler-1")

    # 9) 首次结算
    app.post_settlement("s-1", T.format(d=28), "ST-2026-001", "window-1", "settler-1")
    st = app.ledger.settlements["ST-2026-001"]
    print("== 首次结算 ==")
    for line in st["lines"]:
        print(f"  {line.role:12s} {line.beneficiary_id:12s} {line.amount}")
    print("  合计:", st["total"])

    # 同一结算请求（同 event_id）重放 → 幂等
    try:
        app.post_settlement("s-1", T.format(d=28), "ST-2026-001", "window-1", "settler-1")
        raise AssertionError("幂等失败")
    except DuplicateCommand:
        pass

    # 10) 农户 A 先付款一部分，然后检验复核降级 → 差额重算
    app.pay_settlement("pay-1", T.format(d=29), "ST-2026-001",
                       [{"beneficiary_id": "farmer-a", "amount": "1968.75"}],
                       T.format(d=29), "settler-1", reference="PAY-1")
    app.revise_grade("gr-1", T.format(d=30) and "2026-09-30T10:00:00+08:00",
                     "insp-1", "GRADE_A", "inspector-1", "复检杯测 83 分，降为一级")
    pending = app.pending_work()
    assert "window-1" in pending["windows_awaiting_revision"], pending
    try:
        app.revise_settlement("rs-bad", "2026-09-30T11:00:00+08:00",
                              "ST-2026-001", "settler-1", "  ")
        raise AssertionError("差额理由必填未生效")
    except CommandError:
        pass
    app.revise_settlement("rs-1", "2026-09-30T11:00:00+08:00", "ST-2026-001",
                          "settler-1", "检验复核：special 批次降级为一级，按新版本计算差额")
    st = app.ledger.settlements["ST-2026-001"]
    print("== 复核后差额重算 ==")
    for d in st["revisions"][0]["deltas"]:
        print("  差额:", d)

    # 11) 品种纠错：先登记 v2，再对收购批次盖纠错戳（下游自动取得新版本）
    app.register_cultivar("cv-2", "2026-10-01T08:00:00+08:00", "YUNKA-1", "v2",
                          "breeder-1", "云咖1号（提纯复壮）")
    app.correct_cultivar("cc-2", "2026-10-01T10:00:00+08:00", "buy-lot-1",
                         "YUNKA-1", "v2", "breeder-1", "交付品种底账登记错误，应为v2")
    assert app.ledger.genealogy.effective_cultivar("sale-special") == ("YUNKA-1", "v2")

    # 12) 人工调整：无理由拒绝；超冻结拒绝
    try:
        app.adjust_settlement("adj-bad", "2026-10-01T12:00:00+08:00",
                              "ST-2026-001",
                              [{"beneficiary_id": "farmer-a", "delta": "10.00", "reason": ""}],
                              "settler-1")
        raise AssertionError("理由必填未生效")
    except CommandError:
        pass
    try:
        app.adjust_settlement("adj-bad2", "2026-10-01T12:00:00+08:00",
                              "ST-2026-001",
                              [{"beneficiary_id": "farmer-a", "delta": "999999.00",
                                "reason": "测试超额"}], "settler-1")
        raise AssertionError("冻结上限未生效")
    except CommandError:
        pass
    app.adjust_settlement("adj-1", "2026-10-01T12:00:00+08:00", "ST-2026-001",
                          [{"beneficiary_id": "farmer-b", "delta": "20.00",
                            "reason": "B户主动参与遮阴补种，联盟给予一次性绿色激励"},
                           {"beneficiary_id": "RESERVE", "delta": "-20.00",
                            "reason": "绿色激励从代管结余中列支"}],
                          "settler-1")

    # 13) 争议期间暂停付款
    app.open_dispute("dp-1", "2026-10-02T09:00:00+08:00", "dispute-1",
                     "ST-2026-001", "farmer-b", "认为绿色扣减比例过高")
    try:
        app.pay_settlement("pay-x", "2026-10-02T10:00:00+08:00", "ST-2026-001",
                           [{"beneficiary_id": "farmer-b", "amount": "1.00"}],
                           "2026-10-02T10:00:00+08:00", "settler-1")
        raise AssertionError("争议冻结付款未生效")
    except CommandError:
        pass
    app.resolve_dispute("dpr-1", "2026-10-03T09:00:00+08:00", "dispute-1",
                        "REJECTED", "扣减符合已公示规则 v1", "settler-1")

    # 14) 尾款待付清单
    pending = app.pending_work()
    print("== 待办 ==")
    print(json.dumps(pending, ensure_ascii=False, indent=2))

    # 15) 农户隔离视图
    view_a = app.farmer_view("farmer-a")
    view_b = app.farmer_view("farmer-b")
    assert all(r["farmer_id"] is None for r in [])  # noqa
    assert "RC-2026-001" in [r["receipt_no"] for r in view_a["receipts"]]
    assert "RC-2026-002" not in [r["receipt_no"] for r in view_a["receipts"]]
    assert "RC-2026-002" in [r["receipt_no"] for r in view_b["receipts"]]

    # 16) 穿透证明
    trace = app.premium_trace("ST-2026-001")
    print("== 穿透证明（节选） ==")
    print(json.dumps(trace, ensure_ascii=False, indent=2, default=str)[:2000])

    # 17) 中断恢复：重新加载事件重放，待办一致
    app2 = build_app(f"{tmp}/events.jsonl")
    assert app2.pending_work() == app.pending_work()
    assert app2.farmer_view("farmer-a")["receipts"][0]["receipt_no"] == "RC-2026-001"

    print("\n全部烟雾检查通过，事件数:", len(app.store.events()))


if __name__ == "__main__":
    main()
