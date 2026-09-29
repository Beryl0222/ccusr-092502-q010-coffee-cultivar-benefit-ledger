"""端到端联调演示：一笔品牌溢价如何穿过批次谱系到达履约农户。

运行：

    python3 -m scripts.demo

脚本使用临时 JSONL 事件日志，在“中断”后重新打开同一日志恢复，
全程只依赖标准库。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.ledger import (
    ALLIANCE_ADMIN, FARMER, INSPECTOR, LICENSOR, NURSERY, PROCESSOR,
    SETTLEMENT_CLERK, DomainError, Ledger,
)
from src.store import EventStore

T = {
    "lic_from": "2026-01-01T00:00:00+08:00",
    "lic_to": "2027-01-01T00:00:00+08:00",
    "plant": "2026-03-01T09:00:00+08:00",
    "harvest": "2026-08-10T10:00:00+08:00",
    "deliver": "2026-08-11T08:00:00+08:00",
    "grade": "2026-08-12T08:00:00+08:00",
    "freeze": "2026-09-01T09:00:00+08:00",
}

zhang = {"actor_id": "u-zhang-breeder", "roles": [LICENSOR]}          # 育种单位授权人
li = {"actor_id": "u-li-qc", "roles": [INSPECTOR]}                    # 检验员
wang = {"actor_id": "u-wang-settle", "roles": [SETTLEMENT_CLERK]}     # 结算人
chen = {"actor_id": "u-chen-alliance", "roles": [ALLIANCE_ADMIN]}     # 联盟管理员
nursery = {"actor_id": "u-nursery-01", "roles": [NURSERY]}
jia = {"actor_id": "f-jia", "roles": [FARMER]}
yi = {"actor_id": "f-yi", "roles": [FARMER]}
proc = {"actor_id": "u-proc-q", "roles": [PROCESSOR]}


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def money(fen: int) -> str:
    return f"{fen / 100:.2f} 元"


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="coffee-ledger-")
    log_path = Path(tmpdir) / "events.jsonl"
    print(f"事件日志: {log_path}")

    lg = Ledger(EventStore(log_path, allowed_events=Ledger.EVENTS))

    section("1. 品种版本 → 授权 → 繁育 → 种苗交接 → 地块实种")
    lg.register_cultivar_version(zhang, "cv-y1", "云咖1号", 1,
                                 occurred_at="2026-01-05T09:00:00+08:00")
    lg.register_cultivar_version(zhang, "cv-y2", "云咖1号", 2, supersedes="cv-y1",
                                 occurred_at="2026-01-05T09:05:00+08:00")
    lg.grant_license(zhang, "lic-1", "cv-y1", "普洱片区",
                     T["lic_from"], T["lic_to"], 10_000,
                     occurred_at="2026-01-10T09:00:00+08:00")
    lg.create_nursery_batch(nursery, "nb-1", "cv-y1", "lic-1", 5_000,
                            occurred_at="2026-02-01T09:00:00+08:00")
    lg.transfer_seedlings(nursery, "tr-1", "nb-1", "f-jia", 2_000,
                          occurred_at="2026-02-10T09:00:00+08:00")
    lg.transfer_seedlings(nursery, "tr-2", "nb-1", "f-yi", 1_000,
                          occurred_at="2026-02-10T09:30:00+08:00")
    lg.change_membership(chen, "coop-1", "ACTIVE", reason="成立入会")
    lg.plant_plot(jia, "plot-a", "f-jia", "cv-y1", 2_000,
                  cooperative_id="coop-1", license_id="lic-1", occurred_at=T["plant"])
    lg.plant_plot(yi, "plot-b", "f-yi", "cv-y1", 1_000,
                  cooperative_id="coop-1", license_id="lic-1", occurred_at=T["plant"])
    print("甲 plot-a 实种 2000 株；乙 plot-b 实种 1000 株（均来自繁育批 nb-1 / 授权 lic-1）")

    section("2. 农事与绿色技术核验（乙用药记录断链 → 挂争议）")
    lg.record_farming(jia, "rec-a", "plot-a", "绿色防控+有机肥", T["harvest"])
    lg.verify_green_compliance(li, "ver-a", "rec-a", "plot-a", True)
    lg.record_farming(yi, "rec-b", "plot-b", "有机肥", T["harvest"])
    lg.verify_green_compliance(li, "ver-b-fail", "rec-b", "plot-b", False,
                               note="采购与用药记录断链")
    lg.open_dispute(chen, "disp-green-b", "plot-b", "绿色技术履行断链")
    print("甲核验通过；乙未通过，plot-b 进入待裁决，不参与待结算")

    section("3. 采收（回执幂等）→ 拆分 → 混批 → 入仓")
    lg.harvest(jia, "lot-a", "plot-a", 100_000, "rcpt-1", occurred_at=T["harvest"])
    lg.harvest(yi, "lot-b", "plot-b", 60_000, "rcpt-2", occurred_at=T["harvest"])
    n_before = len(lg.store.events)
    resent = lg.harvest(jia, "lot-a-dup", "plot-a", 999_999, "rcpt-1")
    assert len(lg.store.events) == n_before, "回执重送不得产生新事件"
    print(f"收购回执 rcpt-1 重送：返回原事件 {resent['event_id']}，事件数不变")
    lg.split_lot(jia, "lot-a", [("lot-a1", 40_000), ("lot-a2", 60_000)])
    lg.merge_lots(jia, "lot-m", ["lot-a2", "lot-b"])
    lg.deliver(proc, "d1", "lot-a1", "u-proc-q", "sb-1", 40_000,
               occurred_at=T["deliver"])
    lg.deliver(proc, "d2", "lot-m", "u-proc-q", "sb-1", 120_000,
               occurred_at=T["deliver"])
    print("lot-a 100kg 拆为 40kg/60kg；60kg 与乙 60kg 混批为 lot-m 120kg；d1+d2 共 160kg 入 sb-1")
    print("混批成分:", lg.state.cherry_nodes["lot-m"]["components"])

    section("4. 检验定级 → 品牌溢价冻结 → 规则 → 首轮结算")
    lg.grade(li, "gr-d1", "d1", "AA", 1.2, occurred_at=T["grade"])
    lg.grade(li, "gr-d2", "d2", "A", 1.0, occurred_at=T["grade"])
    lg.freeze_premium(wang, "sb-1", 1_000_000, "品牌:云咖优品",
                      occurred_at=T["freeze"])
    lg.set_rules(wang, "rules-1", 0.7, 0.2, 0.1)
    lg.propose_allocation(wang, "prop-1", "sb-1")
    lg.post_settlement(wang, "st-1", "prop-1",
                       occurred_at="2026-09-05T09:00:00+08:00")
    st = lg.state.settlements["st-1"]
    print(f"冻结品牌溢价 {money(1_000_000)}；规则 农户70%/合作社20%/品种权10%")
    print(f"首轮合格部分过账 {money(st['premium_total_fen'])}（乙挂争议，份额留存冻结池）")
    for line in st["lines"]:
        print(f"  - {line['farmer_id']} 应得 {money(line['amount_fen'])} 依据 {line['basis_refs']}")
    lg.pay_settlement(wang, "st-1", "pay-001")
    print("st-1 已付款（付款事实不可冲销）")

    section("5. 分权拦截（授权人/检验员/结算人不得互相代签）")
    for desc, fn in [
        ("检验员代签授权", lambda: lg.grant_license(li, "x", "cv-y1", "x",
                                                    T["lic_from"], T["lic_to"], 1)),
        ("授权人代签定级", lambda: lg.grade(zhang, "x", "d1", "AA", 1.0)),
        ("结算人无理由人工调整", lambda: lg.manual_adjust(
            wang, "x", "sb-1", [{"farmer_id": "f-yi", "delta_fen": 1}], " ")),
    ]:
        try:
            fn()
        except DomainError as exc:
            print(f"  拒绝 {desc}: {exc}")

    section("6. 断链恢复 + 争议裁决合格 → 新版本差额补发")
    lg.verify_green_compliance(li, "ver-b-pass", "rec-b", "plot-b", True,
                               note="补齐采购与用药记录")
    lg.resolve_dispute(chen, "disp-green-b", "plot-b", "断链已补齐，认定合格",
                       eligible=True)
    lg.post_adjustment(wang, "adj-1", "sb-1", "绿色断链恢复后按新版本重算",
                       occurred_at="2026-09-08T09:00:00+08:00")
    print(f"甲累计入账 {money(lg.state.farmer_settled['f-jia'])}（首轮已付，补发额为 0）")
    print(f"乙差额补发 {money(lg.state.farmer_settled['f-yi'])}")
    print(f"冻结池：{money(lg.state.frozen['sb-1'])}，"
          f"累计净分配 {money(lg.state.allocated['sb-1'])}，"
          f"余额 {money(lg.state.frozen['sb-1'] - lg.state.allocated['sb-1'])}")

    section("7. 检验复核升等 → 再算差额（原等级事件保留）")
    # 复核甲独有的 d1：AA(1.2) → AAA(1.3)。甲加权份额上升应补发；
    # 乙份额相对下降，只能冲减其“尚未付款”的补发余额（st-1 中乙本就无已付款）。
    lg.review_grade(li, "gr-d1-r", "d1", "AAA", 1.3, note="复检杯测升等",
                    occurred_at="2026-09-09T08:00:00+08:00")
    adj2 = lg.post_adjustment(wang, "adj-2", "sb-1", "检验复核后按新等级权重重算")
    for line in adj2["payload"]["lines"]:
        print(f"  - {line['farmer_id']} 差额 {line['delta_fen']} 分"
              f"（正为补发，负为冲减未付余额）")
    print("d1 检验事件序列:",
          [e["event_type"] for e in lg.store.events
           if e["payload"].get("delivery_id") == "d1"])
    print(f"甲入账净额 {money(lg.state.farmer_settled['f-jia'])}，"
          f"其中已付款 {money(lg.state.farmer_paid['f-jia'])}（已付事实未被改写）")

    section("8. 农户接口隔离")
    view = json.dumps(lg.farmer_view("f-jia"), ensure_ascii=False)
    assert "f-yi" not in view and "plot-b" not in view
    print("甲的视图中不包含 f-yi / plot-b；仅含本人地块、交付成分、结算与差额依据")

    section("9. 溢价穿透证明（品牌溢价 → 仓口 → 批次谱系 → 农户）")
    trace = lg.premium_trace("sb-1", farmer_id="f-jia")
    for edge in trace["lineage"][:8]:
        print(f"  {edge['from']} --{edge['qty']}g--> {edge['to']}")
    print(f"守恒校验: {'通过' if trace['conservation_ok'] else '失败'}；"
          f"甲收到 {len(trace['received'])} 笔结算行")

    section("10. 进程中断 → 从 JSONL 重放 → 继续待结算/待裁决")
    del lg
    lg2 = Ledger(EventStore(log_path, allowed_events=Ledger.EVENTS))
    pending = lg2.pending_work()
    print("重放事件数:", len(lg2.store.events))
    print("未决争议:", pending["open_disputes"] or "无")
    print("已过账待付款:", pending["unpaid_posted_settlements"] or "无")
    print("冻结池状态:", pending["frozen_status"])
    # 重放后回执仍幂等
    n = len(lg2.store.events)
    lg2.harvest(jia, "lot-dup2", "plot-a", 1, "rcpt-1")
    assert len(lg2.store.events) == n
    print("重放后回执 rcpt-1 重送仍幂等；待结算与待裁决项可继续处理")


if __name__ == "__main__":
    main()
