"""命令层：校验业务不变量，通过后追加事件并立即投影。

关键约束：
- 职务互斥：良种授权人、检验员、结算人不得为同一主体；
  每类事件只能由对应职务主体签署，不能互相代签。
- 同一 event_id 重送幂等；同一收购回执重送结算请求不会重复结算。
- 拆分/混批/加工数量守恒在谱系层强制。
- 结算总额（含复核差额、人工调整）不得超过冻结销售收益；
  人工调整必须填理由，且不得把任一受益人压到已付金额之下。
- 纠错/复核/资格变更不删除事实，只产生新版本与差额结算。
- 存在未裁决争议的结算单暂停付款。
"""
from __future__ import annotations

import functools
from decimal import Decimal

from .domain import (
    DUTY_ROLES,
    DISPUTE_OPEN,
    DISPUTE_OUTCOMES,
    FlowKind,
    GRADES,
    PARTY_ROLES,
    SIGNING_DUTY,
    money,
    qty,
)
from .ledger import Ledger, LedgerError
from .lineage import LineageError, MIXED_CULTIVAR
from .store import DuplicateEvent, EventStoreError

RESERVE_BENEFICIARY = "RESERVE"  # 联盟代管（绿色扣减/无合作社归属的份额）


class CommandError(ValueError):
    """命令被拒绝。"""


class DuplicateCommand(Exception):
    """同一事件重送，按幂等成功返回。"""

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"重复请求已忽略: {event_id}")


def idempotent(fn):
    """命令入口先判 event_id：已入账则直接按重送返回，不再执行业务校验。"""
    @functools.wraps(fn)
    def wrapper(self, event_id: str, *args, **kwargs):
        if self.store.seen(event_id):
            raise DuplicateCommand(event_id)
        return fn(self, event_id, *args, **kwargs)
    return wrapper


class LedgerApp:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.store = ledger.store

    def _already_frozen_kg(self, lot_id: str) -> Decimal:
        """从已冻结事件派生该批累计冻结千克（中断恢复后仍然准确）。"""
        total = Decimal(0)
        for window in self.ledger.windows.values():
            if not window["frozen"]:
                continue
            for item in window["lots"]:
                if item["lot_id"] == lot_id:
                    total += item["quantity_kg"]
        return qty(total)

    # ---- 内部工具 ----

    def _emit(self, event_type: str, aggregate_id: str, payload: dict,
              event_id: str, occurred_at: str, signer_id: str | None = None) -> dict:
        """校验签名职务后追加事件。返回已入账事件。"""
        if self.store.seen(event_id):
            # 任何命令只要 event_id 相同，一律按重送幂等处理，不重复执行业务校验
            raise DuplicateCommand(event_id)
        duty = SIGNING_DUTY.get(event_type)
        if duty is not None:
            self._require_duty(signer_id, duty, event_type)
        version = self.store.version_of(aggregate_id) + 1
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "aggregate_id": aggregate_id,
            "version": version,
            "payload": payload,
        }
        try:
            self.store.append(event)
        except DuplicateEvent as exc:
            raise DuplicateCommand(exc.event_id) from exc
        except EventStoreError as exc:
            raise CommandError(str(exc)) from exc
        self.ledger.apply_(event)
        return event

    def _require_duty(self, signer_id: str | None, duty: str, what: str) -> None:
        if not signer_id:
            raise CommandError(f"{what} 缺少签名主体")
        party = self.ledger.parties.get(signer_id)
        if party is None:
            raise CommandError(f"签名主体未登记: {signer_id}")
        if duty not in party.duties:
            raise CommandError(f"{what} 需要 {duty} 职务，{signer_id} 无权签署，不能代签")

    def _require_party(self, party_id: str, role: str | None = None) -> None:
        party = self.ledger.parties.get(party_id)
        if party is None:
            raise CommandError(f"主体未登记: {party_id}")
        if role and role not in party.roles:
            raise CommandError(f"主体 {party_id} 不具备 {role} 角色")

    # ---- 主体与品种 ----

    @idempotent
    def register_party(self, event_id: str, occurred_at: str, party_id: str,
                       name: str, roles: list[str], duties: list[str] | None = None) -> dict:
        duties = duties or []
        bad_roles = set(roles) - PARTY_ROLES
        if bad_roles:
            raise CommandError(f"未知主体角色: {sorted(bad_roles)}")
        held = set(duties) & DUTY_ROLES
        if len(held) > 1:
            raise CommandError(
                f"良种授权人、检验员、结算人三类职务互斥，{party_id} 不得同时担任 {sorted(held)}"
            )
        bad_duties = set(duties) - DUTY_ROLES
        if bad_duties:
            raise CommandError(f"未知职务: {sorted(bad_duties)}")
        if party_id in self.ledger.parties:
            raise CommandError(f"主体已登记: {party_id}")
        return self._emit("PARTY_REGISTERED", f"party-{party_id}", {
            "party_id": party_id, "name": name, "roles": sorted(set(roles)),
            "duties": sorted(set(duties)),
        }, event_id, occurred_at)

    @idempotent
    def register_cultivar(self, event_id: str, occurred_at: str, cultivar_id: str,
                          version: str, breeder_id: str, name: str,
                          green_requirements: list[str] | None = None) -> dict:
        self._require_party(breeder_id)
        existing = self.ledger.cultivars.get(cultivar_id)
        if existing is not None:
            # 同一品种的新版本：追加注册事件，breeder 必须一致，旧版本事实保留
            if existing["breeder_id"] != breeder_id:
                raise CommandError("品种新版本育种单位必须与原登记一致")
            if version == existing["version"]:
                raise CommandError(f"品种 {cultivar_id} 版本 {version} 已登记，不能重复")
        return self._emit("CULTIVAR_REGISTERED", f"cultivar-{cultivar_id}", {
            "cultivar_id": cultivar_id, "version": version,
            "breeder_id": breeder_id, "name": name,
            "green_requirements": green_requirements or [],
        }, event_id, occurred_at)

    # ---- 授权 ----

    @idempotent
    def grant_license(self, event_id: str, occurred_at: str, license_id: str,
                      cultivar_id: str, cultivar_version: str, licensor_id: str,
                      nursery_id: str, scope: dict) -> dict:
        self._require_party(nursery_id)
        cultivar = self.ledger.cultivars.get(cultivar_id)
        if cultivar is None:
            raise CommandError(f"品种未登记: {cultivar_id}")
        if license_id in self.ledger.licenses:
            raise CommandError(f"授权已存在: {license_id}")
        # 授权人必须是品种权人（育种单位）或持授权职务，且不能同时是检验员/结算人
        self._require_party(licensor_id)
        if licensor_id != cultivar["breeder_id"]:
            raise CommandError("授权人必须与品种登记的育种单位一致")
        payload = {
            "license_id": license_id, "cultivar_id": cultivar_id,
            "cultivar_version": cultivar_version, "licensor_id": licensor_id,
            "nursery_id": nursery_id, "scope": scope,
        }
        return self._emit("LICENSE_GRANTED", f"license-{license_id}", payload,
                          event_id, occurred_at, signer_id=licensor_id)

    @idempotent
    def amend_license(self, event_id: str, occurred_at: str, license_id: str,
                      licensor_id: str, scope: dict, reason: str) -> dict:
        lic = self.ledger.licenses.get(license_id)
        if lic is None:
            raise CommandError(f"授权不存在: {license_id}")
        return self._emit("LICENSE_AMENDED", f"license-{license_id}", {
            "license_id": license_id, "scope": scope, "reason": reason,
        }, event_id, occurred_at, signer_id=licensor_id)

    # ---- 繁育 / 交接 / 地块 / 农事 ----

    @idempotent
    def establish_nursery_lot(self, event_id: str, occurred_at: str, nursery_lot_id: str,
                              nursery_id: str, license_id: str, quantity_seedlings: str) -> dict:
        lic = self.ledger.licenses.get(license_id)
        if lic is None:
            raise CommandError(f"授权不存在: {license_id}")
        if nursery_id != lic["nursery_id"]:
            raise CommandError("繁育批次必须由授权范围内的苗圃建立")
        if qty(quantity_seedlings) <= 0:
            raise CommandError("繁育数量必须为正")
        if nursery_lot_id in self.ledger.nursery_lots:
            raise CommandError(f"繁育批次已存在: {nursery_lot_id}")
        return self._emit("NURSERY_LOT_ESTABLISHED", f"nursery-lot-{nursery_lot_id}", {
            "nursery_lot_id": nursery_lot_id, "nursery_id": nursery_id,
            "license_id": license_id, "cultivar_id": lic["cultivar_id"],
            "cultivar_version": lic["cultivar_version"],
            "quantity_seedlings": str(qty(quantity_seedlings)),
        }, event_id, occurred_at)

    @idempotent
    def transfer_seedlings(self, event_id: str, occurred_at: str, transfer_id: str,
                           nursery_lot_id: str, from_nursery: str, to_farmer: str,
                           quantity_seedlings: str, transferred_at: str,
                           to_plot_id: str | None = None) -> dict:
        lot = self.ledger.nursery_lots.get(nursery_lot_id)
        if lot is None:
            raise CommandError(f"繁育批次不存在: {nursery_lot_id}")
        amount = qty(quantity_seedlings)
        if amount <= 0:
            raise CommandError("交接数量必须为正")
        if lot["transferred_seedlings"] + amount > lot["quantity_seedlings"]:
            raise CommandError(
                f"交接 {amount} 株后超过繁育批次 {nursery_lot_id} 余量 "
                f"{lot['quantity_seedlings'] - lot['transferred_seedlings']} 株"
            )
        self._require_party(to_farmer)
        return self._emit("SEEDLING_TRANSFERRED", f"transfer-{transfer_id}", {
            "transfer_id": transfer_id, "nursery_lot_id": nursery_lot_id,
            "from_nursery": from_nursery, "to_farmer": to_farmer,
            "to_plot_id": to_plot_id, "quantity_seedlings": str(amount),
            "transferred_at": transferred_at,
        }, event_id, occurred_at)

    @idempotent
    def plant_plot(self, event_id: str, occurred_at: str, plot_id: str, farmer_id: str,
                   transfer_id: str, seedlings: str, planted_at: str,
                   coop_id: str | None = None) -> dict:
        transfer = self.ledger.transfers.get(transfer_id)
        if transfer is None:
            raise CommandError(f"种苗交接不存在: {transfer_id}")
        if transfer["to_farmer"] != farmer_id:
            raise CommandError("地块实种的农户必须与种苗交接接收人一致")
        if plot_id in self.ledger.plots:
            raise CommandError(f"地块已种植: {plot_id}")
        if coop_id is not None:
            self._require_party(coop_id)
        lot = self.ledger.nursery_lots[transfer["nursery_lot_id"]]
        return self._emit("PLOT_PLANTED", f"plot-{plot_id}", {
            "plot_id": plot_id, "farmer_id": farmer_id, "coop_id": coop_id,
            "transfer_id": transfer_id, "cultivar_id": lot["cultivar_id"],
            "cultivar_version": transfer["cultivar_version"],
            "seedlings": str(qty(seedlings)), "planted_at": planted_at,
        }, event_id, occurred_at)

    @idempotent
    def record_green_practice(self, event_id: str, occurred_at: str, plot_id: str,
                              practice_code: str, recorder_id: str, observed_at: str,
                              evidence_ref: str | None = None) -> dict:
        if plot_id not in self.ledger.plots:
            raise CommandError(f"地块不存在: {plot_id}")
        self._require_party(recorder_id)
        return self._emit("GREEN_PRACTICE_RECORDED", f"plot-{plot_id}", {
            "plot_id": plot_id, "practice_code": practice_code,
            "recorder_id": recorder_id, "observed_at": observed_at,
            "evidence_ref": evidence_ref,
        }, event_id, occurred_at)

    # ---- 鲜果交付与批次流转 ----

    @idempotent
    def deliver_cherry(self, event_id: str, occurred_at: str, receipt_no: str,
                       plot_id: str, farmer_id: str, buy_lot_id: str,
                       quantity_kg: str, delivered_at: str) -> dict:
        plot = self.ledger.plots.get(plot_id)
        if plot is None:
            raise CommandError(f"地块不存在: {plot_id}")
        if plot["farmer_id"] != farmer_id:
            raise CommandError("交付农户与地块实种农户不一致，品种谱系不能断链")
        if receipt_no in self.ledger.receipts:
            raise CommandError(f"收购回执号已存在: {receipt_no}")
        if qty(quantity_kg) <= 0:
            raise CommandError("交付数量必须为正")
        return self._emit("CHERRY_DELIVERED", f"receipt-{receipt_no}", {
            "receipt_no": receipt_no, "plot_id": plot_id, "farmer_id": farmer_id,
            "buy_lot_id": buy_lot_id, "quantity_kg": str(qty(quantity_kg)),
            "delivered_at": delivered_at,
        }, event_id, occurred_at)

    @idempotent
    def split_lot(self, event_id: str, occurred_at: str, sources: list[dict],
                  sinks: list[dict]) -> dict:
        return self._flow(event_id, occurred_at, FlowKind.SPLIT, sources, sinks, False)

    @idempotent
    def merge_lots(self, event_id: str, occurred_at: str, sources: list[dict],
                   sink_lot_id: str) -> dict:
        total = sum((qty(s["quantity_kg"]) for s in sources), Decimal(0))
        sinks = [{"lot_id": sink_lot_id, "quantity_kg": str(qty(total))}]
        return self._flow(event_id, occurred_at, FlowKind.MERGE, sources, sinks, False)

    @idempotent
    def transform_lot(self, event_id: str, occurred_at: str, sources: list[dict],
                      sinks: list[dict]) -> dict:
        return self._flow(event_id, occurred_at, FlowKind.TRANSFORM, sources, sinks, True)

    def _flow(self, event_id: str, occurred_at: str, kind: FlowKind,
              sources: list[dict], sinks: list[dict], allow_loss: bool) -> dict:
        src = [(s["lot_id"], qty(s["quantity_kg"])) for s in sources]
        snk = [(s["lot_id"], qty(s["quantity_kg"])) for s in sinks]
        # 落盘前做无副作用守恒预校验；事件投影时谱系再执行一次
        try:
            self.ledger.genealogy.validate_flow(kind, src, snk, allow_loss=allow_loss)
        except LineageError as exc:
            raise CommandError(str(exc)) from exc
        payload = {
            "flow_kind": kind.value,
            "sources": [{"lot_id": lid, "quantity_kg": str(v)} for lid, v in src],
            "sinks": [{"lot_id": lid, "quantity_kg": str(v)} for lid, v in snk],
        }
        agg = "flow-" + "-".join(sorted({lid for lid, _ in src + snk}))
        event_type = {
            FlowKind.SPLIT: "LOT_SPLIT",
            FlowKind.MERGE: "LOT_MERGED",
            FlowKind.TRANSFORM: "LOT_TRANSFORMED",
        }[kind]
        return self._emit(event_type, agg, payload, event_id, occurred_at)

    # ---- 检验 / 复核 / 品种纠错 / 资格变更 ----

    @idempotent
    def grade_lot(self, event_id: str, occurred_at: str, inspection_id: str,
                  lot_id: str, grade: str, inspector_id: str,
                  notes: str = "") -> dict:
        if grade not in GRADES:
            raise CommandError(f"未知等级: {grade}")
        if not self.ledger.genealogy.has_lot(lot_id):
            raise CommandError(f"批次不存在: {lot_id}")
        if inspection_id in self.ledger.grades:
            raise CommandError(f"检验单已存在，复核请使用 revise_grade: {inspection_id}")
        return self._emit("LOT_GRADED", f"inspection-{inspection_id}", {
            "inspection_id": inspection_id, "lot_id": lot_id, "grade": grade,
            "inspector_id": inspector_id, "notes": notes,
        }, event_id, occurred_at, signer_id=inspector_id)

    @idempotent
    def revise_grade(self, event_id: str, occurred_at: str, inspection_id: str,
                     new_grade: str, inspector_id: str, reason: str) -> dict:
        if not reason.strip():
            raise CommandError("检验复核必须填写理由")
        if new_grade not in GRADES:
            raise CommandError(f"未知等级: {new_grade}")
        if inspection_id not in self.ledger.grades:
            raise CommandError(f"检验单不存在: {inspection_id}")
        return self._emit("GRADE_REVISED", f"inspection-{inspection_id}", {
            "inspection_id": inspection_id, "new_grade": new_grade,
            "reason": reason,
        }, event_id, occurred_at, signer_id=inspector_id)

    @idempotent
    def correct_cultivar(self, event_id: str, occurred_at: str, lot_id: str,
                         new_cultivar_id: str, new_version: str,
                         licensor_id: str, reason: str) -> dict:
        if not reason.strip():
            raise CommandError("品种纠错必须填写理由")
        if not self.ledger.genealogy.has_lot(lot_id):
            raise CommandError(f"批次不存在: {lot_id}")
        if new_cultivar_id not in self.ledger.cultivars:
            raise CommandError(f"新品种版本未登记: {new_cultivar_id}@{new_version}")
        if self.ledger.cultivars[new_cultivar_id]["version"] != new_version:
            raise CommandError(
                f"品种 {new_cultivar_id} 当前版本为 "
                f"{self.ledger.cultivars[new_cultivar_id]['version']}，收到 {new_version}"
            )
        return self._emit("CULTIVAR_CORRECTED", f"lot-{lot_id}", {
            "lot_id": lot_id, "new_cultivar_id": new_cultivar_id,
            "new_version": new_version, "reason": reason,
        }, event_id, occurred_at, signer_id=licensor_id)

    @idempotent
    def change_membership(self, event_id: str, occurred_at: str, farmer_id: str,
                          new_coop_id: str | None, effective_at: str,
                          reason: str = "") -> dict:
        self._require_party(farmer_id)
        if new_coop_id is not None:
            self._require_party(new_coop_id)
        return self._emit("MEMBERSHIP_CHANGED", f"membership-{farmer_id}", {
            "farmer_id": farmer_id, "new_coop_id": new_coop_id,
            "effective_at": effective_at, "reason": reason,
        }, event_id, occurred_at)

    # ---- 规则 / 仓口 / 冻结 ----

    @idempotent
    def publish_rule(self, event_id: str, occurred_at: str, rule_id: str,
                     version: int, shares: dict[str, str], grade_rates: dict[str, str],
                     required_practices: list[str] | None = None,
                     green_penalty: str = "1", notes: str = "",
                     settler_id: str | None = None) -> dict:
        total = sum((Decimal(str(v)) for v in shares.values()), Decimal(0))
        if total != Decimal("1"):
            raise CommandError(f"分配比例合计必须为 1，当前为 {total}")
        for grade in grade_rates:
            if grade not in GRADES:
                raise CommandError(f"等级系数含未知等级: {grade}")
            rate = Decimal(str(grade_rates[grade]))
            if rate < 0 or rate > 1:
                raise CommandError("等级系数必须在 0 到 1 之间")
        penalty = Decimal(str(green_penalty))
        if penalty < 0 or penalty > 1:
            raise CommandError("绿色扣减系数必须在 0 到 1 之间")
        if rule_id in self.ledger.rules and self.ledger.rules[rule_id]["version"] >= version:
            raise CommandError(f"规则 {rule_id} 已有版本 {self.ledger.rules[rule_id]['version']}")
        return self._emit("SETTLEMENT_RULE_PUBLISHED", f"rule-{rule_id}", {
            "rule_id": rule_id, "version": version, "shares": shares,
            "grade_rates": grade_rates,
            "required_practices": required_practices or [],
            "green_penalty": green_penalty, "notes": notes,
        }, event_id, occurred_at, signer_id=settler_id)

    @idempotent
    def open_sale_window(self, event_id: str, occurred_at: str, window_id: str,
                         brand_id: str, rule_id: str, mill_id: str | None = None) -> dict:
        self._require_party(brand_id)
        if rule_id not in self.ledger.rules:
            raise CommandError(f"结算规则不存在: {rule_id}")
        if window_id in self.ledger.windows:
            raise CommandError(f"销售仓口已存在: {window_id}")
        return self._emit("SALE_WINDOW_OPENED", f"window-{window_id}", {
            "window_id": window_id, "brand_id": brand_id, "mill_id": mill_id,
            "rule_id": rule_id, "opened_at": occurred_at,
        }, event_id, occurred_at)

    @idempotent
    def freeze_premium(self, event_id: str, occurred_at: str, window_id: str,
                       lots: list[dict], premium_per_kg: str, frozen_total: str,
                       settler_id: str, currency: str = "CNY") -> dict:
        window = self.ledger.windows.get(window_id)
        if window is None:
            raise CommandError(f"销售仓口不存在: {window_id}")
        if window["frozen"]:
            raise CommandError(f"仓口 {window_id} 已冻结，不能重复冻结")
        ppk = money(premium_per_kg)
        if ppk <= 0:
            raise CommandError("每千克溢价必须为正")
        normalized: list[dict] = []
        computed_total = Decimal(0)
        for item in lots:
            lot_id = item["lot_id"]
            if not self.ledger.genealogy.has_lot(lot_id):
                raise CommandError(f"冻结批次不存在: {lot_id}")
            # 品质数据断链的批次不得进入溢价冻结
            self.ledger.grade_for_lot(lot_id)
            # 品种标识混合（异源混批且未纠错）不得主张良种品牌溢价
            cultivar_id, _ver = self.ledger.genealogy.effective_cultivar(lot_id)
            if cultivar_id == MIXED_CULTIVAR:
                raise CommandError(
                    f"批次 {lot_id} 品种来源混合且未纠错，品种标识断链，不能冻结良种溢价"
                )
            amount = qty(item["quantity_kg"])
            born = self.ledger.genealogy._lots[lot_id]["born"]  # noqa: SLF001
            already = self._already_frozen_kg(lot_id)
            if already + amount > born:
                raise CommandError(
                    f"批次 {lot_id} 冻结 {already + amount} 千克超过诞生量 {born}"
                )
            computed_total += money(amount * ppk)
            normalized.append({"lot_id": lot_id, "quantity_kg": str(amount)})
        if money(frozen_total) != money(computed_total):
            raise CommandError(
                f"冻结总额应为 {money(computed_total)}（数量×单价），收到 {money(frozen_total)}"
            )
        return self._emit("PREMIUM_FROZEN", f"window-{window_id}", {
            "window_id": window_id, "lots": normalized,
            "premium_per_kg": str(ppk), "frozen_total": str(money(frozen_total)),
            "frozen_at": occurred_at, "currency": currency,
        }, event_id, occurred_at, signer_id=settler_id)

    # ---- 结算计算引擎 ----

    def _compute_settlement(self, window_id: str) -> dict:
        """依据当前规则版本、检验、品种与资格数据重算整个仓口的分配。"""
        window = self.ledger.windows[window_id]
        rule = self.ledger.rules[window["rule_id"]]
        ppk = window["premium_per_kg"]
        rows = self.ledger.window_receipt_kg(window_id)

        amounts: dict[tuple[str, str], Decimal] = {}  # (受益人, 角色) -> 金额
        receipt_entries: list[dict] = []
        required = set(rule["required_practices"])

        for row in sorted(rows, key=lambda r: (r["receipt_no"], r["grade"])):
            receipt_no = row["receipt_no"]
            receipt = self.ledger.receipts[receipt_no]
            plot = self.ledger.plots[receipt.plot_id]
            kg = row["quantity_kg"]
            grade, inspection_id = row["grade"], row["inspection_id"]
            grade_rate = rule["grade_rates"].get(grade, Decimal(0))
            green_ok = required <= set(plot["practices"])
            # 绿色扣减作用于该鲜果对应的全部分配池：未履约部分进代管，不进任何受益人
            green_factor = Decimal(1) if green_ok else rule["green_penalty"]

            cultivar_id, cultivar_version = self.ledger.genealogy.effective_cultivar(
                receipt.buy_lot_id
            )
            if cultivar_id == MIXED_CULTIVAR:
                raise CommandError(
                    f"回执 {receipt_no} 所在批次品种来源混合且未纠错，品种标识断链，不能结算"
                )
            breeder_id = self.ledger.cultivars[cultivar_id]["breeder_id"]
            coop_id = self.ledger.current_coop(receipt.farmer_id)

            raw = money(kg * ppk * grade_rate)
            earned = money(raw * green_factor)

            for role, beneficiary in (
                ("FARMER", receipt.farmer_id),
                ("COOPERATIVE", coop_id),
                ("BREEDER", breeder_id),
                ("NURSERY", plot["nursery_id"]),
            ):
                share = rule["shares"].get(role, Decimal(0))
                if beneficiary and share:
                    self._add(amounts, (beneficiary, role), money(earned * share))

            receipt_entries.append({
                "receipt_no": receipt_no,
                "quantity_kg": str(kg),
                "grade": grade,
                "inspection_id": inspection_id,
                "grade_rate": str(grade_rate),
                "green_complete": green_ok,
                "green_factor": str(green_factor),
                "cultivar_id": cultivar_id,
                "cultivar_version": cultivar_version,
                "raw_premium": str(raw),
                "earned_premium": str(earned),
                "farmer_id": receipt.farmer_id,
                "coop_id": coop_id,
                "plot_id": receipt.plot_id,
            })

        allocated = sum(amounts.values(), Decimal(0))
        reserve = money(window["frozen_total"] - allocated)
        if reserve < 0:
            raise CommandError(
                f"分配合计 {allocated} 超过冻结销售收益 {window['frozen_total']}"
            )
        lines = [
            {"beneficiary_id": bid, "role": role, "amount": str(money(amount)),
             "basis": [{"window_id": window_id}]}
            for (bid, role), amount in sorted(amounts.items())
        ]
        if reserve > 0:
            lines.append({
                "beneficiary_id": RESERVE_BENEFICIARY, "role": "RESERVE",
                "amount": str(reserve),
                "basis": [{"window_id": window_id, "reason": "未达等级/绿色扣减/舍入结余，代管待退品牌"}],
            })
        return {"lines": lines, "total": str(money(allocated + reserve)),
                "receipts": receipt_entries, "rule_version": rule["version"]}

    @staticmethod
    def _add(bucket: dict, key: tuple[str, str], value: Decimal) -> None:
        bucket[key] = money(bucket.get(key, Decimal(0)) + value)

    def _guard_receipt_kg(self, entries: list[dict]) -> None:
        totals: dict[str, Decimal] = {}
        for entry in entries:
            totals[entry["receipt_no"]] = totals.get(entry["receipt_no"], Decimal(0)) + qty(
                entry["quantity_kg"]
            )
        for receipt_no, amount in totals.items():
            delivered = self.ledger.receipts[receipt_no].quantity_kg
            settled = self.ledger.receipt_settled_kg.get(receipt_no, Decimal(0))
            if settled + amount > delivered + Decimal("0.001"):
                raise CommandError(
                    f"收购回执 {receipt_no} 结算 {settled + amount} 千克超过交付 {delivered} 千克，"
                    "同一回执不得重复结算"
                )

    @idempotent
    def post_settlement(self, event_id: str, occurred_at: str, settlement_id: str,
                        window_id: str, settler_id: str) -> dict:
        if settlement_id in self.ledger.settlements:
            raise CommandError(f"结算单已存在: {settlement_id}")
        window = self.ledger.windows.get(window_id)
        if window is None:
            raise CommandError(f"销售仓口不存在: {window_id}")
        if not window["frozen"]:
            raise CommandError("仓口尚未冻结溢价，不能结算")
        calc = self._compute_settlement(window_id)
        self._guard_receipt_kg(calc["receipts"])
        return self._emit("SETTLEMENT_POSTED", f"settlement-{settlement_id}", {
            "settlement_id": settlement_id, "window_id": window_id,
            "rule_id": window["rule_id"], "rule_version": calc["rule_version"],
            "lines": calc["lines"], "total": calc["total"],
            "receipts": calc["receipts"],
        }, event_id, occurred_at, signer_id=settler_id)

    @idempotent
    def revise_settlement(self, event_id: str, occurred_at: str, settlement_id: str,
                          settler_id: str, reason: str) -> dict:
        """检验复核 / 品种纠错 / 资格变更后按新版本重算，差额版本入账，不动旧事实。"""
        if not reason.strip():
            raise CommandError("差额结算必须填写变更理由")
        st = self.ledger.settlements.get(settlement_id)
        if st is None:
            raise CommandError(f"结算单不存在: {settlement_id}")
        window = self.ledger.windows[st["window_id"]]
        calc = self._compute_settlement(st["window_id"])
        # 已付款事实不能删除：重算后若新应得低于已付，差额记为待追回（负差额），
        # 不阻止新版本入账；由后续追回/裁决处理。
        proposed = {l["beneficiary_id"]: money(l["amount"]) for l in calc["lines"]}
        overpaid = []
        for beneficiary_id, paid in st["paid_amounts"].items():
            gap = money(paid - proposed.get(beneficiary_id, Decimal(0)))
            if gap > Decimal("0.001"):
                overpaid.append({"beneficiary_id": beneficiary_id,
                                 "paid": str(paid),
                                 "new_amount": str(proposed.get(beneficiary_id, Decimal(0))),
                                 "receivable": str(gap)})
        if money(calc["total"]) > window["frozen_total"]:
            raise CommandError("重算总额超过冻结销售收益")
        basis_versions = {
            "rule_version": calc["rule_version"],
            "grade_versions": {
                e["receipt_no"]: self.ledger.grades[e["inspection_id"]].version
                for e in calc["receipts"]
            },
            "overpaid": overpaid,
        }
        return self._emit("SETTLEMENT_REVISED", f"settlement-{settlement_id}", {
            "settlement_id": settlement_id, "window_id": st["window_id"],
            "rule_id": window["rule_id"], "rule_version": calc["rule_version"],
            "lines": calc["lines"], "total": calc["total"],
            "receipts": calc["receipts"], "reason": reason,
            "basis_versions": basis_versions,
        }, event_id, occurred_at, signer_id=settler_id)

    @idempotent
    def adjust_settlement(self, event_id: str, occurred_at: str, settlement_id: str,
                          adjustments: list[dict], settler_id: str) -> dict:
        """人工调整分配比例：每项必须有理由；调整后不得超过冻结收益、不得低于已付。"""
        st = self.ledger.settlements.get(settlement_id)
        if st is None:
            raise CommandError(f"结算单不存在: {settlement_id}")
        if not adjustments:
            raise CommandError("调整内容为空")
        normalized = []
        current = {l.beneficiary_id: l.amount for l in st["lines"]}
        for item in adjustments:
            reason = item.get("reason", "")
            if not reason.strip():
                raise CommandError("人工调整必须填写理由")
            bid = item["beneficiary_id"]
            delta = money(item["delta"])
            normalized.append({"beneficiary_id": bid, "delta": str(delta), "reason": reason})
            current[bid] = money(current.get(bid, Decimal(0)) + delta)
        window = self.ledger.windows[st["window_id"]]
        new_total = money(sum(current.values(), Decimal(0)))
        if new_total > window["frozen_total"]:
            raise CommandError(
                f"调整后合计 {new_total} 超过冻结销售收益 {window['frozen_total']}"
            )
        paid = st["paid_amounts"]
        # 人工调整不能通过负向调整把受益人进一步压到其已付金额之下；
        # 复核已经形成的超付走"待追回"，不阻止其他人的调整
        adjusted = {item["beneficiary_id"]: money(item["delta"]) for item in adjustments}
        for bid, delta in adjusted.items():
            new_amount = current.get(bid, Decimal(0))
            if delta < 0 and new_amount + Decimal("0.001") < paid.get(bid, Decimal(0)):
                raise CommandError(
                    f"调整将使 {bid} 分得 {new_amount} 低于已付 {paid.get(bid, 0)}，"
                    "已付款事实不能被人工调整推翻"
                )
        return self._emit("SETTLEMENT_ADJUSTED", f"settlement-{settlement_id}", {
            "settlement_id": settlement_id, "adjustments": normalized,
            "signer": settler_id,
        }, event_id, occurred_at, signer_id=settler_id)

    @idempotent
    def pay_settlement(self, event_id: str, occurred_at: str, settlement_id: str,
                       amounts: list[dict], paid_at: str, settler_id: str,
                       reference: str | None = None) -> dict:
        st = self.ledger.settlements.get(settlement_id)
        if st is None:
            raise CommandError(f"结算单不存在: {settlement_id}")
        open_disputes = self.ledger.open_disputes_for(settlement_id)
        if open_disputes:
            raise CommandError(
                f"结算单存在 {len(open_disputes)} 笔未裁决争议，暂停付款: "
                + ", ".join(d["dispute_id"] for d in open_disputes)
            )
        line_amounts = {l.beneficiary_id: l.amount for l in st["lines"]}
        normalized = []
        for item in amounts:
            bid = item["beneficiary_id"]
            amount = money(item["amount"])
            if amount <= 0:
                raise CommandError("付款金额必须为正")
            already = st["paid_amounts"].get(bid, Decimal(0))
            if already + amount > line_amounts.get(bid, Decimal(0)) + Decimal("0.001"):
                raise CommandError(
                    f"{bid} 累计付款 {already + amount} 超过应得 {line_amounts.get(bid, 0)}"
                )
            normalized.append({"beneficiary_id": bid, "amount": str(amount)})
        return self._emit("SETTLEMENT_PAID", f"settlement-{settlement_id}", {
            "settlement_id": settlement_id, "amounts": normalized,
            "paid_at": paid_at, "reference": reference,
        }, event_id, occurred_at, signer_id=settler_id)

    # ---- 争议 ----

    @idempotent
    def open_dispute(self, event_id: str, occurred_at: str, dispute_id: str,
                     settlement_id: str, claimant_id: str, claim: str) -> dict:
        if settlement_id not in self.ledger.settlements:
            raise CommandError(f"结算单不存在: {settlement_id}")
        self._require_party(claimant_id)
        if dispute_id in self.ledger.disputes:
            raise CommandError(f"争议已存在: {dispute_id}")
        if not claim.strip():
            raise CommandError("争议诉求不能为空")
        return self._emit("DISPUTE_OPENED", f"dispute-{dispute_id}", {
            "dispute_id": dispute_id, "settlement_id": settlement_id,
            "claimant_id": claimant_id, "claim": claim, "opened_at": occurred_at,
        }, event_id, occurred_at)

    @idempotent
    def resolve_dispute(self, event_id: str, occurred_at: str, dispute_id: str,
                        outcome: str, resolution_note: str, settler_id: str) -> dict:
        if outcome not in DISPUTE_OUTCOMES:
            raise CommandError(f"裁决结论必须是 {sorted(DISPUTE_OUTCOMES)}")
        dispute = self.ledger.disputes.get(dispute_id)
        if dispute is None:
            raise CommandError(f"争议不存在: {dispute_id}")
        if dispute["status"] != DISPUTE_OPEN:
            raise CommandError("争议已裁决")
        return self._emit("DISPUTE_RESOLVED", f"dispute-{dispute_id}", {
            "dispute_id": dispute_id, "outcome": outcome,
            "resolution_note": resolution_note, "resolved_at": occurred_at,
        }, event_id, occurred_at, signer_id=settler_id)

    # ===== 读侧接口 =====

    def farmer_view(self, farmer_id: str) -> dict:
        """农户接口：只能看到自己的交付、农事依据与分给自己的结算行。"""
        self._require_party(farmer_id)
        my_receipts = []
        for receipt_no in sorted(self.ledger.receipts):
            receipt = self.ledger.receipts[receipt_no]
            if receipt.farmer_id != farmer_id:
                continue
            plot = self.ledger.plots[receipt.plot_id]
            lot = receipt.buy_lot_id
            grade_info = None
            try:
                grade_info = self.ledger.grade_for_lot(lot)
            except LedgerError:
                grade_info = None
            my_receipts.append({
                "receipt_no": receipt_no,
                "plot_id": receipt.plot_id,
                "quantity_kg": str(receipt.quantity_kg),
                "delivered_at": receipt.delivered_at,
                "cultivar_version": self.ledger.genealogy.effective_cultivar(lot)[1],
                "grade": grade_info[0] if grade_info else None,
                "green_practices": sorted(plot["practices"]),
                "settled_kg": str(self.ledger.receipt_settled_kg.get(receipt_no, Decimal(0))),
            })
        my_settlements = []
        for st_id in sorted(self.ledger.settlements):
            st = self.ledger.settlements[st_id]
            line = next((l for l in st["lines"] if l.beneficiary_id == farmer_id), None)
            if line is None:
                continue
            my_settlements.append({
                "settlement_id": st_id,
                "window_id": st["window_id"],
                "status": st["status"],
                "amount": str(line.amount),
                "paid": str(st["paid_amounts"].get(farmer_id, Decimal(0))),
                "my_receipts": [e["receipt_no"] for e in st["receipt_entries"]
                                if e.get("farmer_id") == farmer_id],
                "rule_version": st.get("rule_version"),
            })
        return {
            "farmer_id": farmer_id,
            "coop_id": self.ledger.current_coop(farmer_id),
            "receipts": my_receipts,
            "settlements": my_settlements,
        }

    def premium_trace(self, settlement_id: str) -> dict:
        """联盟穿透证明：一笔品牌溢价如何沿批次谱系到达农户。"""
        st = self.ledger.settlements.get(settlement_id)
        if st is None:
            raise CommandError(f"结算单不存在: {settlement_id}")
        window = self.ledger.windows[st["window_id"]]
        frozen = [
            {"lot_id": item["lot_id"], "quantity_kg": str(item["quantity_kg"]),
             "grade": self.ledger.grade_for_lot(item["lot_id"])[0],
             "inspection_id": self.ledger.grade_for_lot(item["lot_id"])[1],
             "upward_path": self.ledger.genealogy.upward(item["lot_id"]),
             "receipt_share": {
                 r: str(v) for r, v in self.ledger.genealogy.receipts(item["lot_id"]).items()
             }}
            for item in window["lots"]
        ]
        farmer_lines = [
            {"beneficiary_id": l.beneficiary_id, "amount": str(l.amount)}
            for l in st["lines"] if l.role == "FARMER"
        ]
        return {
            "settlement_id": settlement_id,
            "window_id": st["window_id"],
            "frozen_event": window.get("frozen_event"),
            "posted_event": st["posted_event"],
            "revision_events": [r["event_id"] for r in st["revisions"]],
            "premium_per_kg": str(window["premium_per_kg"]),
            "frozen_total": str(window["frozen_total"]),
            "settled_total": str(st["total"]),
            "rule_version": st.get("rule_version"),
            "frozen_lots": frozen,
            "farmer_lines": farmer_lines,
            "paid": {bid: str(v) for bid, v in st["paid_amounts"].items()},
        }

    def pending_work(self) -> dict:
        """中断恢复后的待办：待结算仓口、待重算仓口、待裁决争议、待付款结算单。"""
        settled_windows = {st["window_id"] for st in self.ledger.settlements.values()}
        frozen_windows = {w for w, win in self.ledger.windows.items() if win["frozen"]}
        pending_settle = sorted(frozen_windows - settled_windows)
        pending_revise = sorted(self.ledger.dirty_windows & settled_windows)
        pending_disputes = sorted(
            d_id for d_id, d in self.ledger.disputes.items() if d["status"] == DISPUTE_OPEN
        )
        pending_recovery: list[dict] = []
        for st_id, st in self.ledger.settlements.items():
            for rev in st["revisions"]:
                pending_recovery.extend(
                    {"settlement_id": st_id, **item}
                    for item in rev.get("basis_versions", {}).get("overpaid", [])
                )
        pending_pay = []
        for st_id, st in self.ledger.settlements.items():
            if self.ledger.open_disputes_for(st_id):
                continue
            unpaid = [
                l.beneficiary_id for l in st["lines"]
                if st["paid_amounts"].get(l.beneficiary_id, Decimal(0)) < l.amount - Decimal("0.001")
            ]
            if unpaid:
                pending_pay.append({"settlement_id": st_id, "unpaid_beneficiaries": unpaid})
        return {
            "windows_awaiting_settlement": pending_settle,
            "windows_awaiting_revision": pending_revise,
            "disputes_awaiting_ruling": pending_disputes,
            "settlements_awaiting_payment": pending_pay,
            "overpaid_awaiting_recovery": pending_recovery,
        }
