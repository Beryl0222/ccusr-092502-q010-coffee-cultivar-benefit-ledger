"""测试夹具：构造一条从品种到冻结仓口的完整合法链路。"""
from __future__ import annotations

import json
from pathlib import Path

from src.commands import LedgerApp
from src.ledger import Ledger
from src.store import EventStore

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "domain.json").read_text("utf-8")
)


def build_app(path: str) -> LedgerApp:
    store = EventStore(path, set(CONTRACT["events"]))
    store.load()
    ledger = Ledger(store)
    ledger.replay()
    return LedgerApp(ledger)


def seed_parties_and_goods(app: LedgerApp, day0: int = 1) -> None:
    T = f"2026-09-{{d:02d}}T09:00:00+08:00"
    app.register_party("e-breeder", T.format(d=day0), "breeder-1", "热作所",
                       ["BREEDER"], ["LICENSOR"])
    app.register_party("e-nursery", T.format(d=day0), "nursery-1", "绿洲苗圃", ["NURSERY"])
    app.register_party("e-farmer-a", T.format(d=day0), "farmer-a", "岩温", ["FARMER"])
    app.register_party("e-farmer-b", T.format(d=day0), "farmer-b", "玉应", ["FARMER"])
    app.register_party("e-coop", T.format(d=day0), "coop-1", "合作社", ["COOPERATIVE"])
    app.register_party("e-brand", T.format(d=day0), "brand-1", "品牌方", ["BRAND"])
    app.register_party("e-inspector", T.format(d=day0), "inspector-1", "质检员",
                       ["ALLIANCE"], ["INSPECTOR"])
    app.register_party("e-settler", T.format(d=day0), "settler-1", "结算员",
                       ["ALLIANCE"], ["SETTLER"])

    app.register_cultivar("e-cv", T.format(d=day0 + 1), "YUNKA-1", "v1",
                          "breeder-1", "云咖1号",
                          green_requirements=["SHADE_TREE", "ORGANIC_FERTILIZER"])
    app.grant_license("e-lic", T.format(d=day0 + 1), "license-1", "YUNKA-1", "v1",
                      "breeder-1", "nursery-1", {"region": "普洱"})
    app.establish_nursery_lot("e-nl", T.format(d=day0 + 2), "nlot-1", "nursery-1",
                              "license-1", "100000")
    app.transfer_seedlings("e-tr1", T.format(d=day0 + 3), "transfer-1", "nlot-1",
                           "nursery-1", "farmer-a", "3000",
                           T.format(d=day0 + 3), to_plot_id="plot-a")
    app.transfer_seedlings("e-tr2", T.format(d=day0 + 3), "transfer-2", "nlot-1",
                           "nursery-1", "farmer-b", "2500",
                           T.format(d=day0 + 3), to_plot_id="plot-b")
    app.plant_plot("e-pl1", T.format(d=day0 + 4), "plot-a", "farmer-a", "transfer-1",
                   "3000", T.format(d=day0 + 4), coop_id="coop-1")
    app.plant_plot("e-pl2", T.format(d=day0 + 4), "plot-b", "farmer-b", "transfer-2",
                   "2500", T.format(d=day0 + 4), coop_id="coop-1")


def seed_full_chain(app: LedgerApp) -> None:
    """交付 1000+600，混批→加工1280→拆分900/380，检验、规则、冻结。"""
    seed_parties_and_goods(app)
    T = "2026-09-{d:02d}T09:00:00+08:00"
    # farmer-a 绿色齐全，farmer-b 只做遮阴
    app.record_green_practice("e-gp1", T.format(d=10), "plot-a", "SHADE_TREE", "coop-1",
                              T.format(d=10), evidence_ref="ev1")
    app.record_green_practice("e-gp2", T.format(d=10), "plot-a", "ORGANIC_FERTILIZER",
                              "coop-1", T.format(d=10), evidence_ref="ev2")
    app.record_green_practice("e-gp3", T.format(d=10), "plot-b", "SHADE_TREE", "coop-1",
                              T.format(d=10), evidence_ref="ev3")
    app.deliver_cherry("e-d1", T.format(d=20), "R-001", "plot-a", "farmer-a",
                       "buy-1", "1000", T.format(d=20))
    app.deliver_cherry("e-d2", T.format(d=20), "R-002", "plot-b", "farmer-b",
                       "buy-1", "600", T.format(d=20))
    app.merge_lots("e-m1", T.format(d=21),
                   [{"lot_id": "buy-1", "quantity_kg": "1600"}], "washed")
    app.transform_lot("e-t1", T.format(d=22),
                      [{"lot_id": "washed", "quantity_kg": "1600"}],
                      [{"lot_id": "green", "quantity_kg": "1280"}])
    app.split_lot("e-s1", T.format(d=23),
                  [{"lot_id": "green", "quantity_kg": "1280"}],
                  [{"lot_id": "sale-special", "quantity_kg": "900"},
                   {"lot_id": "sale-a", "quantity_kg": "380"}])
    app.grade_lot("e-g1", T.format(d=24), "insp-1", "sale-special", "SPECIAL",
                  "inspector-1")
    app.grade_lot("e-g2", T.format(d=24), "insp-2", "sale-a", "GRADE_A", "inspector-1")
    app.publish_rule("e-rule", T.format(d=25), "rule-1", 1,
                     shares={"FARMER": "0.70", "COOPERATIVE": "0.10",
                             "BREEDER": "0.12", "NURSERY": "0.08"},
                     grade_rates={"SPECIAL": "1.0", "GRADE_A": "0.6", "REJECT": "0"},
                     required_practices=["SHADE_TREE", "ORGANIC_FERTILIZER"],
                     green_penalty="0.5", settler_id="settler-1")
    app.open_sale_window("e-win", T.format(d=26), "window-1", "brand-1", "rule-1")
    app.freeze_premium("e-frz", T.format(d=27), "window-1",
                       [{"lot_id": "sale-special", "quantity_kg": "900"},
                        {"lot_id": "sale-a", "quantity_kg": "380"}],
                       "4.00", "5120.00", settler_id="settler-1")
