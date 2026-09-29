"""导出一条完整中文联调样例：事件流 + 关键读模型，写入 data/sample_flow.json。

运行：PYTHONPATH=. python3 scripts/export_sample.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.fixtures import build_app, seed_full_chain

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    tmp = tempfile.mkdtemp()
    path = f"{tmp}/events.jsonl"
    app = build_app(path)
    seed_full_chain(app)

    T = "2026-09-{d:02d}T09:00:00+08:00"
    app.post_settlement("x-s1", T.format(d=28), "ST-2026-001", "window-1",
                        "settler-1")
    app.pay_settlement("x-pay1", T.format(d=29), "ST-2026-001",
                       [{"beneficiary_id": "farmer-a", "amount": "1974.00"}],
                       T.format(d=29), "settler-1", reference="BANK-PAY-001")
    app.revise_grade("x-gr1", "2026-09-30T10:00:00+08:00", "insp-1", "GRADE_A",
                     "inspector-1", "复检杯测 83 分，special 降为一级")
    app.revise_settlement("x-rs1", "2026-09-30T11:00:00+08:00", "ST-2026-001",
                          "settler-1", "检验复核降级，按新版本计算差额")
    app.open_dispute("x-dp1", "2026-10-02T09:00:00+08:00", "dispute-1",
                     "ST-2026-001", "farmer-b", "绿色扣减比例异议")

    out = {
        "说明": (
            "咖啡良种权益分配账端到端联调样例：品种→授权→繁育→交接→地块→"
            "绿色农事→交付→混批/加工/拆分（数量守恒）→检验→冻结→结算→付款→"
            "复核差额→争议。events 为仅追加事件流，其余为重放得到的读模型。"
        ),
        "events": app.store.events(),
        "farmer_view_farmer_a": app.farmer_view("farmer-a"),
        "premium_trace_ST_2026_001": app.premium_trace("ST-2026-001"),
        "pending_work": app.pending_work(),
    }
    target = ROOT / "data" / "sample_flow.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {len(out['events'])} 个事件到 {target}")


if __name__ == "__main__":
    main()
