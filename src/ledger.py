"""良种权益分配账：事件重放投影与读模型。

状态只能由事件得到：命令层（commands 模块）校验不变量后追加事件，
本模块把事件逐条投影成可查询的状态。进程中断后重新重放事件即可恢复，
待结算与待裁决项都是状态的派生结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .domain import DISPUTE_OPEN, money, qty
from .lineage import Genealogy
from .store import EventStore


class LedgerError(ValueError):
    """业务不变量被违反。"""


@dataclass
class Party:
    party_id: str
    name: str
    roles: set[str] = field(default_factory=set)
    duties: set[str] = field(default_factory=set)


@dataclass
class Receipt:
    receipt_no: str
    plot_id: str
    farmer_id: str
    buy_lot_id: str
    quantity_kg: Decimal
    delivered_at: str


@dataclass
class Inspection:
    inspection_id: str
    lot_id: str
    grade: str
    inspector_id: str
    version: int
    history: list[dict] = field(default_factory=list)


@dataclass
class SettlementLine:
    beneficiary_id: str
    role: str
    amount: Decimal
    basis: list[dict] = field(default_factory=list)


class Ledger:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.parties: dict[str, Party] = {}
        self.cultivars: dict[str, dict] = {}          # cultivar_id -> 当前版本信息
        self.licenses: dict[str, dict] = {}
        self.nursery_lots: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.plots: dict[str, dict] = {}
        # farmer_id -> [{"coop_id": str|None, "effective_at": iso}]，首项为当前资格
        self.memberships: dict[str, list[dict]] = {}
        self.receipts: dict[str, Receipt] = {}
        self.genealogy = Genealogy()
        self.grades: dict[str, Inspection] = {}       # inspection_id
        self.lot_grade: dict[str, str] = {}           # lot_id -> inspection_id（最新生效）
        self.windows: dict[str, dict] = {}
        self.rules: dict[str, dict] = {}              # rule_id -> 当前版本
        self.settlements: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {}
        # 收购回执 -> 已进入的结算单列表（穿透与审计用）
        self.receipt_settled_in: dict[str, list[str]] = {}
        # 收购回执 -> 已结算千克累计（不得超过交付量）
        self.receipt_settled_kg: dict[str, Decimal] = {}
        # 冻结后被纠错/复核/资格变更弄脏、待重算差额的仓口
        self.dirty_windows: set[str] = set()

    # ---- 重放 ----

    def replay(self) -> None:
        for event in self.store.events():
            self.apply_(event)

    def apply_(self, event: dict) -> None:
        etype = event["event_type"]
        p = event["payload"]
        getattr(self, f"_on_{etype.lower()}")(event["aggregate_id"], p, event)

    # ---- 主体 / 品种 / 授权 ----

    def _on_party_registered(self, agg: str, p: dict, e: dict) -> None:
        self.parties[p["party_id"]] = Party(
            party_id=p["party_id"], name=p["name"],
            roles=set(p["roles"]), duties=set(p.get("duties", [])),
        )

    def _on_cultivar_registered(self, agg: str, p: dict, e: dict) -> None:
        self.cultivars[p["cultivar_id"]] = {
            "cultivar_id": p["cultivar_id"],
            "version": p["version"],
            "breeder_id": p["breeder_id"],
            "name": p["name"],
            "green_requirements": list(p.get("green_requirements", [])),
            "event_id": e["event_id"],
        }

    def _on_license_granted(self, agg: str, p: dict, e: dict) -> None:
        self.licenses[p["license_id"]] = {
            "license_id": p["license_id"],
            "cultivar_id": p["cultivar_id"],
            "cultivar_version": p["cultivar_version"],
            "licensor_id": p["licensor_id"],
            "nursery_id": p["nursery_id"],
            "scope": dict(p["scope"]),
            "amendments": [],
            "granted_event": e["event_id"],
        }

    def _on_license_amended(self, agg: str, p: dict, e: dict) -> None:
        lic = self.licenses[p["license_id"]]
        lic["scope"].update(p.get("scope", {}))
        lic["amendments"].append({"scope": dict(p.get("scope", {})),
                                  "reason": p["reason"], "event_id": e["event_id"]})

    # ---- 繁育 / 交接 / 地块 / 农事 ----

    def _on_nursery_lot_established(self, agg: str, p: dict, e: dict) -> None:
        self.nursery_lots[p["nursery_lot_id"]] = {
            "nursery_lot_id": p["nursery_lot_id"],
            "nursery_id": p["nursery_id"],
            "license_id": p["license_id"],
            "cultivar_id": p["cultivar_id"],
            "cultivar_version": p["cultivar_version"],
            "quantity_seedlings": qty(p["quantity_seedlings"]),
            "transferred_seedlings": Decimal(0),
        }

    def _on_seedling_transferred(self, agg: str, p: dict, e: dict) -> None:
        lot = self.nursery_lots[p["nursery_lot_id"]]
        lot["transferred_seedlings"] += qty(p["quantity_seedlings"])
        self.transfers[p["transfer_id"]] = {
            "transfer_id": p["transfer_id"],
            "nursery_lot_id": p["nursery_lot_id"],
            "nursery_id": lot["nursery_id"],
            "cultivar_version": lot["cultivar_version"],
            "from_nursery": p["from_nursery"],
            "to_farmer": p["to_farmer"],
            "to_plot_id": p.get("to_plot_id"),
            "quantity_seedlings": qty(p["quantity_seedlings"]),
            "transferred_at": p["transferred_at"],
            "event_id": e["event_id"],
        }

    def _on_plot_planted(self, agg: str, p: dict, e: dict) -> None:
        self.plots[p["plot_id"]] = {
            "plot_id": p["plot_id"],
            "farmer_id": p["farmer_id"],
            "coop_id_at_planting": p.get("coop_id"),
            "transfer_id": p["transfer_id"],
            "nursery_id": self.transfers[p["transfer_id"]]["nursery_id"],
            "cultivar_id": p["cultivar_id"],
            "cultivar_version": p["cultivar_version"],
            "seedlings": qty(p["seedlings"]),
            "planted_at": p["planted_at"],
            "practices": {},   # practice_code -> {recorded_at, evidence_ref}
        }

    def _on_green_practice_recorded(self, agg: str, p: dict, e: dict) -> None:
        plot = self.plots[p["plot_id"]]
        plot["practices"][p["practice_code"]] = {
            "recorded_at": p["observed_at"],
            "recorder_id": p["recorder_id"],
            "evidence_ref": p.get("evidence_ref"),
            "event_id": e["event_id"],
        }

    # ---- 鲜果交付与批次流转 ----

    def _on_cherry_delivered(self, agg: str, p: dict, e: dict) -> None:
        plot = self.plots[p["plot_id"]]
        self.genealogy.deliver(
            p["plot_id"], p["buy_lot_id"], qty(p["quantity_kg"]),
            p["receipt_no"], plot["cultivar_id"], plot["cultivar_version"],
        )
        self.receipts[p["receipt_no"]] = Receipt(
            receipt_no=p["receipt_no"], plot_id=p["plot_id"],
            farmer_id=p["farmer_id"], buy_lot_id=p["buy_lot_id"],
            quantity_kg=qty(p["quantity_kg"]), delivered_at=p["delivered_at"],
        )

    def _flow_lots(self, p: dict, allow_loss: bool) -> None:
        from .domain import FlowKind
        kind = FlowKind(p.get("flow_kind", "SPLIT"))
        sources = [(s["lot_id"], qty(s["quantity_kg"])) for s in p["sources"]]
        sinks = [(s["lot_id"], qty(s["quantity_kg"])) for s in p["sinks"]]
        self.genealogy.flow(kind, sources, sinks, allow_loss=allow_loss)

    def _on_lot_split(self, agg: str, p: dict, e: dict) -> None:
        self._flow_lots(p, allow_loss=False)

    def _on_lot_merged(self, agg: str, p: dict, e: dict) -> None:
        self._flow_lots(p, allow_loss=False)

    def _on_lot_transformed(self, agg: str, p: dict, e: dict) -> None:
        self._flow_lots(p, allow_loss=True)

    # ---- 检验 / 纠错 / 资格 ----

    def _on_lot_graded(self, agg: str, p: dict, e: dict) -> None:
        insp = Inspection(
            inspection_id=p["inspection_id"], lot_id=p["lot_id"],
            grade=p["grade"], inspector_id=p["inspector_id"], version=1,
            history=[{"grade": p["grade"], "reason": p.get("notes", ""),
                      "event_id": e["event_id"], "occurred_at": e["occurred_at"]}],
        )
        self.grades[p["inspection_id"]] = insp
        self.lot_grade[p["lot_id"]] = p["inspection_id"]

    def _on_grade_revised(self, agg: str, p: dict, e: dict) -> None:
        insp = self.grades[p["inspection_id"]]
        insp.grade = p["new_grade"]
        insp.version += 1
        insp.history.append({"grade": p["new_grade"], "reason": p["reason"],
                             "event_id": e["event_id"], "occurred_at": e["occurred_at"]})
        self._mark_touch_windows_dirty(insp.lot_id)

    def _on_cultivar_corrected(self, agg: str, p: dict, e: dict) -> None:
        self.genealogy.correct_cultivar(
            p["lot_id"], p["new_cultivar_id"], p["new_version"]
        )
        self._mark_touch_windows_dirty(p["lot_id"])

    def _on_membership_changed(self, agg: str, p: dict, e: dict) -> None:
        chain = self.memberships.setdefault(p["farmer_id"], [])
        chain.insert(0, {"coop_id": p.get("new_coop_id"),
                         "effective_at": p["effective_at"],
                         "reason": p.get("reason", ""), "event_id": e["event_id"]})
        # 该农户交付过的批次相关仓口都需要重算合作社份额
        for plot_id, plot in self.plots.items():
            if plot["farmer_id"] == p["farmer_id"]:
                for edge in self.genealogy.deliveries_of_plot(plot_id):
                    lot_id = edge.sinks[0][0]
                    self._mark_touch_windows_dirty(lot_id)

    def _mark_touch_windows_dirty(self, lot_id: str) -> None:
        """纠错/复核发生后，凡冻结范围覆盖该批（含下游）的仓口标记待重算。"""
        affected = {lot_id} | set(self.genealogy.downward(lot_id))
        for window_id, window in self.windows.items():
            if not window.get("frozen"):
                continue
            frozen_lots = {item["lot_id"] for item in window["lots"]}
            if frozen_lots & affected:
                self.dirty_windows.add(window_id)

    # ---- 销售仓口 / 冻结 / 规则 ----

    def _on_sale_window_opened(self, agg: str, p: dict, e: dict) -> None:
        self.windows[p["window_id"]] = {
            "window_id": p["window_id"], "brand_id": p["brand_id"],
            "mill_id": p.get("mill_id"), "rule_id": p["rule_id"],
            "opened_at": p["opened_at"], "frozen": False,
            "lots": [], "premium_per_kg": None, "frozen_total": Decimal(0),
        }

    def _on_premium_frozen(self, agg: str, p: dict, e: dict) -> None:
        window = self.windows[p["window_id"]]
        window["frozen"] = True
        window["lots"] = [{"lot_id": s["lot_id"], "quantity_kg": qty(s["quantity_kg"])}
                          for s in p["lots"]]
        window["premium_per_kg"] = money(p["premium_per_kg"])
        window["currency"] = p.get("currency", "CNY")
        window["frozen_total"] = money(p["frozen_total"])
        window["frozen_at"] = p["frozen_at"]
        window["frozen_event"] = e["event_id"]

    def _on_settlement_rule_published(self, agg: str, p: dict, e: dict) -> None:
        self.rules[p["rule_id"]] = {
            "rule_id": p["rule_id"],
            "version": p["version"],
            "shares": {k: Decimal(str(v)) for k, v in p["shares"].items()},
            "grade_rates": {k: Decimal(str(v)) for k, v in p.get("grade_rates", {}).items()},
            "required_practices": list(p.get("required_practices", [])),
            "green_penalty": Decimal(str(p.get("green_penalty", "1"))),
            "notes": p.get("notes", ""),
            "event_id": e["event_id"],
        }

    # ---- 结算 ----

    def _record_settlement_lines(self, settlement_id: str, lines: list[dict],
                                 total: Decimal, receipt_entries: list[dict],
                                 window_id: str, rule_version: int) -> None:
        st = self.settlements[settlement_id]
        st["lines"] = [SettlementLine(beneficiary_id=l["beneficiary_id"], role=l["role"],
                                      amount=money(l["amount"]), basis=l.get("basis", []))
                       for l in lines]
        st["total"] = money(total)
        st["receipt_entries"] = list(receipt_entries)
        st["window_id"] = window_id
        st["rule_version"] = rule_version

    def _on_settlement_posted(self, agg: str, p: dict, e: dict) -> None:
        self.settlements[p["settlement_id"]] = {
            "settlement_id": p["settlement_id"],
            "window_id": p["window_id"], "rule_id": p["rule_id"],
            "posted_event": e["event_id"], "posted_at": e["occurred_at"],
            "revisions": [], "adjustments": [], "payments": [],
            "paid_amounts": {},   # beneficiary -> 已付
            "status": "POSTED",
        }
        self._record_settlement_lines(
            p["settlement_id"], p["lines"], p["total"], p["receipts"],
            p["window_id"], p["rule_version"],
        )
        for entry in p["receipts"]:
            receipt_no = entry["receipt_no"]
            self.receipt_settled_in.setdefault(receipt_no, []).append(p["settlement_id"])
            self.receipt_settled_kg[receipt_no] = qty(
                self.receipt_settled_kg.get(receipt_no, Decimal(0))
                + Decimal(str(entry["quantity_kg"]))
            )
        # 首次入账后该仓口不再是待结算
        self.dirty_windows.discard(p["window_id"])

    def _on_settlement_revised(self, agg: str, p: dict, e: dict) -> None:
        st = self.settlements[p["settlement_id"]]
        old_lines = {l.beneficiary_id: l.amount for l in st["lines"]}
        self._record_settlement_lines(
            st["settlement_id"], p["lines"], p["total"], p["receipts"],
            st["window_id"], p["rule_version"],
        )
        deltas = []
        for line in st["lines"]:
            delta = line.amount - old_lines.get(line.beneficiary_id, Decimal(0))
            if delta:
                deltas.append({"beneficiary_id": line.beneficiary_id,
                               "old_amount": str(money(old_lines.get(line.beneficiary_id, 0))),
                               "new_amount": str(line.amount), "delta": str(money(delta))})
        st["revisions"].append({
            "reason": p["reason"], "deltas": deltas,
            "basis_versions": p.get("basis_versions", {}),
            "event_id": e["event_id"], "occurred_at": e["occurred_at"],
        })
        st["status"] = "REVISED"
        self.dirty_windows.discard(st["window_id"])

    def _on_settlement_adjusted(self, agg: str, p: dict, e: dict) -> None:
        st = self.settlements[p["settlement_id"]]
        index = {l.beneficiary_id: l for l in st["lines"]}
        applied = []
        for a in p["adjustments"]:
            line = index[a["beneficiary_id"]]
            line.amount = money(line.amount + Decimal(str(a["delta"])))
            applied.append({"beneficiary_id": a["beneficiary_id"], "delta": a["delta"],
                            "reason": a["reason"]})
        st["total"] = money(sum((l.amount for l in st["lines"]), Decimal(0)))
        st["adjustments"].append({"items": applied, "signer": p["signer"],
                                  "event_id": e["event_id"],
                                  "occurred_at": e["occurred_at"]})

    def _on_settlement_paid(self, agg: str, p: dict, e: dict) -> None:
        st = self.settlements[p["settlement_id"]]
        paid = st["paid_amounts"]
        for item in p["amounts"]:
            paid[item["beneficiary_id"]] = money(
                paid.get(item["beneficiary_id"], Decimal(0)) + Decimal(str(item["amount"]))
            )
        st["payments"].append({
            "amounts": [{"beneficiary_id": i["beneficiary_id"], "amount": i["amount"]}
                        for i in p["amounts"]],
            "paid_at": p["paid_at"], "reference": p.get("reference"),
            "event_id": e["event_id"],
        })
        st["status"] = "PAID"

    # ---- 争议 ----

    def _on_dispute_opened(self, agg: str, p: dict, e: dict) -> None:
        self.disputes[p["dispute_id"]] = {
            "dispute_id": p["dispute_id"], "settlement_id": p["settlement_id"],
            "claimant_id": p["claimant_id"], "claim": p["claim"],
            "status": DISPUTE_OPEN, "opened_at": p["opened_at"],
            "open_event": e["event_id"], "resolutions": [],
        }

    def _on_dispute_resolved(self, agg: str, p: dict, e: dict) -> None:
        d = self.disputes[p["dispute_id"]]
        d["status"] = p["outcome"]
        d["resolutions"].append({"outcome": p["outcome"], "note": p.get("resolution_note", ""),
                                 "resolved_at": p["resolved_at"], "event_id": e["event_id"]})

    # ===== 读模型 =====

    def current_coop(self, farmer_id: str) -> str | None:
        chain = self.memberships.get(farmer_id)
        if not chain:
            plot = next((pl for pl in self.plots.values() if pl["farmer_id"] == farmer_id), None)
            return plot["coop_id_at_planting"] if plot else None
        return chain[0]["coop_id"]

    def grade_for_lot(self, lot_id: str) -> tuple[str, str]:
        """沿该批次向上谱系找最近的生效检验：返回 (等级, inspection_id)。

        检验通常挂在加工/销售批次上，结算时从冻结批次向源头找，
        找不到说明这条谱系的品质数据断链，不能结算。
        """
        for node in [lot_id] + self.genealogy.upward(lot_id):
            if node in self.lot_grade:
                insp = self.grades[self.lot_grade[node]]
                return insp.grade, insp.inspection_id
        raise LedgerError(f"批次 {lot_id} 向上谱系没有检验等级，品质数据断链，不能结算")

    def window_receipt_kg(self, window_id: str) -> list[dict]:
        """冻结销售批次摊回收购回执，按 (回执, 检验等级) 分行。

        每行：{"receipt_no", "quantity_kg", "grade", "inspection_id",
        "farmer_id", "plot_id"}。加工损耗按比例自动摊到各回执。
        """
        window = self.windows[window_id]
        acc: dict[tuple[str, str], dict] = {}
        for item in window["lots"]:
            grade, inspection_id = self.grade_for_lot(item["lot_id"])
            for receipt_no, value in self.genealogy.receipts(item["lot_id"]).items():
                key = (receipt_no, grade)
                row = acc.setdefault(key, {
                    "receipt_no": receipt_no, "quantity_kg": Decimal(0),
                    "grade": grade, "inspection_id": inspection_id,
                })
                row["quantity_kg"] = qty(row["quantity_kg"] + value)
        rows = list(acc.values())
        for row in rows:
            receipt = self.receipts[row["receipt_no"]]
            row["farmer_id"] = receipt.farmer_id
            row["plot_id"] = receipt.plot_id
        return rows

    def open_disputes_for(self, settlement_id: str) -> list[dict]:
        return [d for d in self.disputes.values()
                if d["settlement_id"] == settlement_id and d["status"] == DISPUTE_OPEN]
