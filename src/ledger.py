"""咖啡良种权益分配账 —— 事件溯源领域内核。

只依赖标准库。设计要点见 ``contracts/domain.json`` 的 invariants：

* 所有业务状态都是事件的投影，命令只负责校验并追加事件；
* 谱系（繁育 → 交接 → 地块 → 鲜果 → 仓口/销售）以带数量的有向边保存，
  拆分与混批在投影时逐地块成分守恒；
* 纠错、复核、资格改变不删除旧事实，另立事件；结算时一律按“当前最新版本”
  重算合格性与金额，与已入账事实求差额，已付款事实保持原样；
* 授权人 / 检验员 / 结算人三类敏感职权在 actor_id 维度互斥，且互斥台账
  本身也是事件投影，进程重启后从日志重建；
* 冻结溢价是分配上限：任何时点累计净分配（农户+合作社+品种权分成，
  含差额与人工调整）不得超过冻结池；
* 收购回执幂等；绿色断链等进入争议，裁决后按新版本补发或冲回。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .store import EventStore

# ---- 常量 ---------------------------------------------------------------

LICENSOR = "LICENSOR"
INSPECTOR = "INSPECTOR"
SETTLEMENT_CLERK = "SETTLEMENT_CLERK"
ALLIANCE_ADMIN = "ALLIANCE_ADMIN"
NURSERY = "NURSERY"
FARMER = "FARMER"
PROCESSOR = "PROCESSOR"

# 担任过其中任一敏感角色的自然人，不得再担任另外两类（禁止互相代签）。
_SENSITIVE = {LICENSOR, INSPECTOR, SETTLEMENT_CLERK}

_EVENTS = [
    "CULTIVAR_VERSION_REGISTERED",
    "LICENSE_GRANTED",
    "LICENSE_SCOPE_AMENDED",
    "NURSERY_BATCH_CREATED",
    "SEEDLING_TRANSFERRED",
    "PLOT_PLANTED",
    "FARMING_RECORDED",
    "GREEN_COMPLIANCE_VERIFIED",
    "MEMBERSHIP_CHANGED",
    "CHERRY_HARVESTED",
    "CHERRY_LOT_SPLIT",
    "CHERRY_LOT_MERGED",
    "CHERRY_DELIVERED",
    "LOT_GRADED",
    "GRADE_REVIEWED",
    "CULTIVAR_CORRECTED",
    "DISPUTE_OPENED",
    "DISPUTE_RESOLVED",
    "PREMIUM_FROZEN",
    "ALLOCATION_RULES_SET",
    "PREMIUM_ALLOCATION_PROPOSED",
    "SETTLEMENT_POSTED",
    "SETTLEMENT_PAID",
    "SETTLEMENT_ADJUSTMENT_POSTED",
    "MANUAL_SHARE_ADJUSTED",
]

# 事件 → 唯一可签发角色
_ROLE_GUARD: dict[str, str] = {
    "CULTIVAR_VERSION_REGISTERED": LICENSOR,
    "LICENSE_GRANTED": LICENSOR,
    "LICENSE_SCOPE_AMENDED": LICENSOR,
    "GREEN_COMPLIANCE_VERIFIED": INSPECTOR,
    "LOT_GRADED": INSPECTOR,
    "GRADE_REVIEWED": INSPECTOR,
    "PREMIUM_FROZEN": SETTLEMENT_CLERK,
    "ALLOCATION_RULES_SET": SETTLEMENT_CLERK,
    "PREMIUM_ALLOCATION_PROPOSED": SETTLEMENT_CLERK,
    "SETTLEMENT_POSTED": SETTLEMENT_CLERK,
    "SETTLEMENT_PAID": SETTLEMENT_CLERK,
    "SETTLEMENT_ADJUSTMENT_POSTED": SETTLEMENT_CLERK,
    "MANUAL_SHARE_ADJUSTED": SETTLEMENT_CLERK,
}

_ADMIN_EVENTS = {
    "CULTIVAR_CORRECTED",
    "MEMBERSHIP_CHANGED",
    "DISPUTE_OPENED",
    "DISPUTE_RESOLVED",
}


class DomainError(RuntimeError):
    """业务规则被违反。"""


# ---- 纯函数工具 ----------------------------------------------------------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _add_components(total: dict[str, int], parts: dict[str, int]) -> None:
    for plot_id, qty in parts.items():
        total[plot_id] = total.get(plot_id, 0) + qty


def largest_remainder(amount: int, weights: dict[str, float]) -> dict[str, int]:
    """最大余数法把整数 ``amount`` 按权重分尽，结果求和恒等于 amount。"""
    total = sum(weights.values())
    if amount == 0 or total <= 0:
        return {key: 0 for key in weights}
    raw = {key: amount * w / total for key, w in weights.items()}
    result = {key: int(v) for key, v in raw.items()}
    left = amount - sum(result.values())
    for key, _ in sorted(raw.items(), key=lambda x: (-(x[1] - int(x[1])), x[0])):
        if left <= 0:
            break
        result[key] += 1
        left -= 1
    if left != 0:
        raise DomainError("整数分配尾差无法收敛")
    return result


def scale_components(components: dict[str, int], child_weight: int,
                     parent_weight: int) -> dict[str, int]:
    """按重量比例拆分各地块成分，整数克用最大余数法，分尽 child_weight。"""
    if parent_weight <= 0:
        raise DomainError("父批重量必须为正")
    weights = {plot_id: qty * child_weight / parent_weight
               for plot_id, qty in components.items()}
    return largest_remainder(child_weight, weights)


# ---- 投影 ----------------------------------------------------------------

class LedgerState:
    """事件日志的内存投影。所有字段只在 ``apply`` 中变化。"""

    def __init__(self) -> None:
        # 品种与授权
        self.cultivar_versions: dict[str, dict[str, Any]] = {}
        self.licenses: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # 繁育与种苗
        self.nursery_batches: dict[str, dict[str, Any]] = {}
        self.seedling_out: dict[str, int] = defaultdict(int)
        self.produced_per_license: dict[str, int] = defaultdict(int)
        self.seedlings_held: dict[str, int] = defaultdict(int)
        # 地块、农事、绿色核验、资格
        self.plots: dict[str, dict[str, Any]] = {}
        self.green_passed: dict[str, set[str]] = defaultdict(set)
        self.green_failed: dict[str, set[str]] = defaultdict(set)
        self.membership: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # 鲜果谱系
        self.cherry_nodes: dict[str, dict[str, Any]] = {}
        self.consumed_lots: set[str] = set()   # 已被混批/入仓消耗
        self.harvest_by_receipt: dict[str, str] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        # 争议
        self.lot_disputes: dict[str, list[str]] = defaultdict(list)
        self.plot_disputes: dict[str, set[str]] = defaultdict(set)
        # 检验与品种纠错（只增版本）
        self.grades: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.plot_cultivar: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # 仓口、冻结、规则、提案
        self.delivery_lots: dict[str, list[str]] = defaultdict(list)
        self.posted_deliveries: set[str] = set()
        self.frozen: dict[str, int] = {}
        self.allocated: dict[str, int] = defaultdict(int)   # 冻结池累计净分配
        self.rules: dict[str, Any] | None = None
        self.proposals: dict[str, dict[str, Any]] = {}
        self.posted_proposals: set[str] = set()
        # 结算
        self.settlements: dict[str, dict[str, Any]] = {}
        self.adjustments: list[dict[str, Any]] = []
        self.manual_adjustments: list[dict[str, Any]] = []
        # 各销售批次下：农户/合作社/品种权累计已分配净额
        self.farmer_batch: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.coop_batch: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.royalty_batch: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # 农户全局已入账 / 已付款
        self.farmer_settled: dict[str, int] = defaultdict(int)
        self.farmer_paid: dict[str, int] = defaultdict(int)
        # 分权台账（事件投影，重启后重建）：actor_id → 已行使敏感角色
        self.used_roles: dict[str, set[str]] = defaultdict(set)

    # -- 当前版本查询 ----------------------------------------------------

    def current_cultivar(self, plot_id: str) -> str:
        """地块当前生效的品种版本：实种值叠加全部纠错（纠错不删旧版本）。"""
        current = self.plots[plot_id]["cultivar_version_id"]
        for change in self.plot_cultivar.get(plot_id, []):
            current = change["cultivar_version_id"]
        return current

    def current_license(self, license_id: str) -> dict[str, Any]:
        return self.licenses[license_id][-1]

    def current_membership(self, cooperative_id: str) -> dict[str, Any] | None:
        history = self.membership.get(cooperative_id)
        return history[-1] if history else None

    def latest_grade(self, delivery_id: str) -> dict[str, Any]:
        history = self.grades[delivery_id]
        if not history:
            raise DomainError(f"交付 {delivery_id} 尚无检验等级")
        return history[-1]

    # -- 合格性（一律按当前最新版本重算）----------------------------------

    def eligibility(self, plot_id: str, delivery: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        plot = self.plots[plot_id]
        cv = self.current_cultivar(plot_id)
        license_id = plot.get("license_id")
        if license_id and self.licenses.get(license_id):
            lic = self.current_license(license_id)
            if cv != lic["cultivar_version_id"]:
                reasons.append("品种版本不在授权范围")
            if not (lic["valid_from"] <= delivery["delivered_at"] <= lic["valid_to"]):
                reasons.append("交付时点超出授权期限")
            if lic["status"] != "ACTIVE":
                reasons.append("授权已中止")
        if self.green_passed.get(plot_id):
            pass
        elif self.green_failed.get(plot_id):
            reasons.append("绿色技术核验未通过")
        else:
            reasons.append("绿色技术核验缺失")
        coop = plot.get("cooperative_id")
        if coop:
            rec = self.current_membership(coop)
            if rec is None or rec["status"] != "ACTIVE":
                reasons.append("合作社资格当前无效")
        if self.plot_disputes.get(plot_id):
            reasons.append("存在未决争议")
        if self.lot_disputes.get(delivery["delivery_id"]):
            reasons.append("交付存在未决争议")
        return (not reasons, reasons)

    def lineage(self, sale_or_lot_id: str) -> list[dict[str, Any]]:
        """反向追溯谱系边：销售仓口 → 交付 → 鲜果批（拆/混）→ 采收。"""
        edges: list[dict[str, Any]] = []
        stack = [sale_or_lot_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.deliveries or current in self.delivery_lots:
                for delivery_id in self.delivery_lots.get(current, []):
                    d = self.deliveries[delivery_id]
                    edges.append({"from": delivery_id, "to": current, "qty": d["weight_g"]})
                    stack.append(delivery_id)
                if current in self.deliveries:
                    d = self.deliveries[current]
                    edges.append({"from": d["cherry_lot_id"], "to": current,
                                  "qty": d["weight_g"]})
                    stack.append(d["cherry_lot_id"])
                continue
            node = self.cherry_nodes.get(current)
            if node is None:
                continue
            for parent_id, qty in node["inputs"]:
                edges.append({"from": parent_id, "to": current, "qty": qty})
                stack.append(parent_id)
        return edges

    # -- 事件应用 --------------------------------------------------------

    def apply(self, event: dict[str, Any]) -> None:  # noqa: C901 - 投影集中一处
        etype = event["event_type"]
        p = event["payload"]
        at = event["occurred_at"]
        actor = event.get("actor")

        # 分权台账是已发生事实的投影：谁签过哪类敏感职权，重启后仍可查。
        if actor and etype in _ROLE_GUARD:
            self.used_roles[actor["actor_id"]].add(_ROLE_GUARD[etype])

        if etype == "CULTIVAR_VERSION_REGISTERED":
            self.cultivar_versions[event["aggregate_id"]] = {
                "cultivar_version_id": event["aggregate_id"], **p}

        elif etype in ("LICENSE_GRANTED", "LICENSE_SCOPE_AMENDED"):
            self.licenses[event["aggregate_id"]].append(
                {"license_id": event["aggregate_id"], "occurred_at": at, **p})

        elif etype == "NURSERY_BATCH_CREATED":
            self.nursery_batches[event["aggregate_id"]] = {
                "nursery_batch_id": event["aggregate_id"], **p}
            self.produced_per_license[p["license_id"]] += p["produced_seedlings"]

        elif etype == "SEEDLING_TRANSFERRED":
            self.seedling_out[p["nursery_batch_id"]] += p["quantity"]
            self.seedlings_held[p["to_farmer_id"]] += p["quantity"]

        elif etype == "PLOT_PLANTED":
            self.seedlings_held[p["farmer_id"]] -= p["seedling_count"]
            self.plots[event["aggregate_id"]] = {
                "plot_id": event["aggregate_id"], "farmer_id": p["farmer_id"],
                "cooperative_id": p.get("cooperative_id"),
                "cultivar_version_id": p["cultivar_version_id"],
                "license_id": p.get("license_id"),
                "seedling_count": p["seedling_count"], "planted_at": at}

        elif etype == "FARMING_RECORDED":
            pass  # 农事事实可在绿色核验事件中按需引用，投影只保留核验结论

        elif etype == "GREEN_COMPLIANCE_VERIFIED":
            target = self.green_passed if p["passed"] else self.green_failed
            target[p["plot_id"]].add(p["record_id"])
            other = self.green_failed if p["passed"] else self.green_passed
            other[p["plot_id"]].discard(p["record_id"])

        elif etype == "MEMBERSHIP_CHANGED":
            self.membership[p["cooperative_id"]].append(
                {"status": p["status"], "since": at, "reason": p.get("reason", "")})

        elif etype == "CHERRY_HARVESTED":
            self.cherry_nodes[event["aggregate_id"]] = {
                "kind": "harvest", "plot_id": p["plot_id"], "weight_g": p["weight_g"],
                "components": {p["plot_id"]: p["weight_g"]},
                "inputs": [], "occurred_at": at}
            self.harvest_by_receipt[p["receipt_id"]] = event["aggregate_id"]

        elif etype == "CHERRY_LOT_SPLIT":
            parent = self.cherry_nodes[p["source_lot_id"]]
            taken_total = 0
            for child in p["children"]:
                comp = scale_components(parent["components"], child["weight_g"],
                                        parent["weight_g"])
                self.cherry_nodes[child["lot_id"]] = {
                    "kind": "split", "weight_g": child["weight_g"],
                    "components": comp,
                    "inputs": [(p["source_lot_id"], child["weight_g"])],
                    "occurred_at": at}
                taken_total += child["weight_g"]
            if taken_total > parent["weight_g"]:
                raise DomainError("拆分超过父批重量")  # 命令层已挡，投影兜底
            remaining = scale_components(
                parent["components"], parent["weight_g"] - taken_total,
                parent["weight_g"]) if parent["weight_g"] - taken_total else {}
            self.cherry_nodes[p["source_lot_id"]] = {**parent, "components": remaining,
                                                     "weight_g": parent["weight_g"] - taken_total}

        elif etype == "CHERRY_LOT_MERGED":
            components: dict[str, int] = {}
            inputs = []
            for src in p["source_lot_ids"]:
                node = self.cherry_nodes[src]
                _add_components(components, node["components"])
                inputs.append((src, node["weight_g"]))
            self.cherry_nodes[event["aggregate_id"]] = {
                "kind": "merge", "weight_g": p["weight_g"], "components": components,
                "inputs": inputs, "occurred_at": at}
            for src in p["source_lot_ids"]:
                self.consumed_lots.add(src)

        elif etype == "CHERRY_DELIVERED":
            node = self.cherry_nodes[p["cherry_lot_id"]]
            self.deliveries[event["aggregate_id"]] = {
                "delivery_id": event["aggregate_id"],
                "cherry_lot_id": p["cherry_lot_id"],
                "processor_id": p["processor_id"],
                "sale_batch_id": p["sale_batch_id"],
                "weight_g": p["weight_g"], "delivered_at": at,
                "components": dict(node["components"])}
            self.delivery_lots[p["sale_batch_id"]].append(event["aggregate_id"])
            self.consumed_lots.add(p["cherry_lot_id"])
            self.cherry_nodes.setdefault(p["sale_batch_id"], {
                "kind": "sale", "weight_g": 0, "components": {}, "inputs": [],
                "occurred_at": at})

        elif etype in ("LOT_GRADED", "GRADE_REVIEWED"):
            self.grades[p["delivery_id"]].append({
                "grade": p["grade"], "weight_factor": p["weight_factor"],
                "inspector_id": p["inspector_id"], "occurred_at": at,
                "kind": etype, "note": p.get("note", "")})

        elif etype == "CULTIVAR_CORRECTED":
            self.plot_cultivar[p["plot_id"]].append(
                {"cultivar_version_id": p["cultivar_version_id"],
                 "reason": p.get("reason", ""), "occurred_at": at})

        elif etype == "DISPUTE_OPENED":
            self.lot_disputes[p["ref_id"]].append(event["aggregate_id"])
            node = self.cherry_nodes.get(p["ref_id"])
            plot_ids: list[str] = []
            if node is not None:
                plot_ids = list(node["components"])
            elif p["ref_id"] in self.deliveries:
                plot_ids = list(self.deliveries[p["ref_id"]]["components"])
            elif p["ref_id"] in self.plots:
                plot_ids = [p["ref_id"]]
            for plot_id in plot_ids:
                self.plot_disputes[plot_id].add(event["aggregate_id"])

        elif etype == "DISPUTE_RESOLVED":
            self.lot_disputes[p["ref_id"]].remove(event["aggregate_id"]) \
                if event["aggregate_id"] in self.lot_disputes[p["ref_id"]] else None
            node = self.cherry_nodes.get(p["ref_id"])
            candidates = (list(node["components"]) if node is not None else [])
            if p["ref_id"] in self.deliveries:
                candidates += list(self.deliveries[p["ref_id"]]["components"])
            if p["ref_id"] in self.plots:
                candidates.append(p["ref_id"])
            for plot_id in candidates:
                self.plot_disputes[plot_id].discard(event["aggregate_id"])

        elif etype == "PREMIUM_FROZEN":
            self.frozen[event["aggregate_id"]] = p["premium_fen"]
            self.cherry_nodes.setdefault(event["aggregate_id"], {
                "kind": "sale", "weight_g": 0, "components": {}, "inputs": [],
                "occurred_at": at})

        elif etype == "ALLOCATION_RULES_SET":
            self.rules = {"rules_id": event["aggregate_id"], "occurred_at": at, **p}

        elif etype == "PREMIUM_ALLOCATION_PROPOSED":
            self.proposals[event["aggregate_id"]] = {
                "proposal_id": event["aggregate_id"], "occurred_at": at, **p}

        elif etype == "SETTLEMENT_POSTED":
            self.settlements[event["aggregate_id"]] = {
                "settlement_id": event["aggregate_id"], "status": "POSTED",
                "posted_at": at, "paid_at": None, **p}
            for d_id in p["delivery_ids"]:
                self.posted_deliveries.add(d_id)
            self.posted_proposals.add(p["proposal_id"])
            self.allocated[p["sale_batch_id"]] += p["premium_total_fen"]
            for line in p["lines"]:
                self.farmer_batch[p["sale_batch_id"]][line["farmer_id"]] += line["amount_fen"]
                self.farmer_settled[line["farmer_id"]] += line["amount_fen"]
            for line in p.get("coop_lines", []):
                self.coop_batch[p["sale_batch_id"]][line["cooperative_id"]] += line["amount_fen"]
            for line in p.get("royalty_lines", []):
                self.royalty_batch[p["sale_batch_id"]][line["cultivar_version_id"]] += line["amount_fen"]

        elif etype == "SETTLEMENT_PAID":
            s = self.settlements[event["aggregate_id"]]
            s["status"] = "PAID"
            s["paid_at"] = at
            s["payment_ref"] = p["payment_ref"]
            for line in s["lines"]:
                self.farmer_paid[line["farmer_id"]] += line["amount_fen"]

        elif etype == "SETTLEMENT_ADJUSTMENT_POSTED":
            self.adjustments.append(
                {"adjustment_id": event["aggregate_id"], "occurred_at": at, **p})
            net = 0
            for line in p["lines"]:
                self.farmer_batch[p["sale_batch_id"]][line["farmer_id"]] += line["delta_fen"]
                self.farmer_settled[line["farmer_id"]] += line["delta_fen"]
                net += line["delta_fen"]
            for line in p.get("coop_lines", []):
                self.coop_batch[p["sale_batch_id"]][line["cooperative_id"]] += line["delta_fen"]
                net += line["delta_fen"]
            for line in p.get("royalty_lines", []):
                self.royalty_batch[p["sale_batch_id"]][line["cultivar_version_id"]] += line["delta_fen"]
                net += line["delta_fen"]
            self.allocated[p["sale_batch_id"]] += net

        elif etype == "MANUAL_SHARE_ADJUSTED":
            self.manual_adjustments.append(
                {"adjustment_id": event["aggregate_id"], "occurred_at": at, **p})
            net = 0
            for line in p["lines"]:
                self.farmer_batch[p["sale_batch_id"]][line["farmer_id"]] += line["delta_fen"]
                self.farmer_settled[line["farmer_id"]] += line["delta_fen"]
                net += line["delta_fen"]
            self.allocated[p["sale_batch_id"]] += net


# ---- 内核外观 ------------------------------------------------------------

class Ledger:
    """命令式外观：校验业务不变量并向 EventStore 追加事件。"""

    EVENTS = tuple(_EVENTS)

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore(allowed_events=_EVENTS)
        self.state = LedgerState()
        # 先补放历史事件，再订阅后续事件
        for event in self.store.events:
            self.state.apply(event)
        self.store.subscribe(self.state.apply)
        # 序号从历史 event_id 中恢复，避免重启后与已接收事件撞号
        self._seq = 0
        for event in self.store.events:
            tail = event["event_id"].rsplit("-", 1)[-1]
            if tail.isdigit():
                self._seq = max(self._seq, int(tail))

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    # -- 通用追加 --------------------------------------------------------

    def _append(self, event_type: str, aggregate_id: str, payload: dict[str, Any],
                actor: dict[str, Any] | None = None,
                idempotency_key: str | None = None,
                version: int | None = None) -> dict[str, Any]:
        self._require_actor(event_type, actor)
        occurred_at = payload.pop("_occurred_at", None) or _now()
        event: dict[str, Any] = {
            "event_id": self._next_id("evt"),
            "event_type": event_type,
            "occurred_at": occurred_at,
            "aggregate_id": aggregate_id,
            "version": version or self.store.version_of(aggregate_id) + 1,
            "payload": payload,
        }
        if actor is not None:
            event["actor"] = {"actor_id": actor["actor_id"], "roles": list(actor["roles"])}
        if idempotency_key is not None:
            event["idempotency_key"] = idempotency_key
        return self.store.append(event)

    @staticmethod
    def _require_actor(event_type: str, actor: dict[str, Any] | None) -> None:
        if actor is None or not isinstance(actor.get("actor_id"), str) \
                or not actor["actor_id"].strip():
            raise DomainError(f"{event_type} 必须携带非空签发人 actor.actor_id")
        roles = actor.get("roles")
        if not isinstance(roles, list) or not roles:
            raise DomainError(f"{event_type} 必须携带 actor.roles")
        required = _ROLE_GUARD.get(event_type)
        if required is not None and required not in roles:
            raise DomainError(f"{event_type} 仅可由 {required} 签发，当前角色 {roles}")
        if event_type in _ADMIN_EVENTS and ALLIANCE_ADMIN not in roles:
            raise DomainError(f"{event_type} 仅可由 {ALLIANCE_ADMIN} 签发")

    def _check_separation(self, actor: dict[str, Any]) -> None:
        """敏感角色互斥：同一 actor_id 签过另一类敏感职权即拒绝。"""
        roles = set(actor["roles"]) & _SENSITIVE
        ever = self.state.used_roles.get(actor["actor_id"], set())
        for role in roles:
            conflict = ever - {role}
            if conflict:
                raise DomainError(
                    f"actor {actor['actor_id']} 已行使过 {sorted(conflict)} 职权，"
                    f"不得再以 {role} 身份代签")

    # -- 品种与授权 ------------------------------------------------------

    def register_cultivar_version(self, actor: dict[str, Any], cultivar_version_id: str,
                                  name: str, revision: int, *, supersedes: str | None = None,
                                  occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        if cultivar_version_id in self.state.cultivar_versions:
            raise DomainError("品种版本已登记")
        payload: dict[str, Any] = {"name": name, "revision": revision,
                                   "supersedes": supersedes}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("CULTIVAR_VERSION_REGISTERED", cultivar_version_id, payload, actor)

    def grant_license(self, actor: dict[str, Any], license_id: str,
                      cultivar_version_id: str, scope_area: str,
                      valid_from: str, valid_to: str, max_seedlings: int,
                      *, occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        if self.state.licenses.get(license_id):
            raise DomainError("授权已存在，范围变更请使用 amend_license")
        payload = {"cultivar_version_id": cultivar_version_id, "scope_area": scope_area,
                   "valid_from": valid_from, "valid_to": valid_to,
                   "max_seedlings": max_seedlings, "status": "ACTIVE",
                   "licensor_id": actor["actor_id"]}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("LICENSE_GRANTED", license_id, payload, actor)

    def amend_license(self, actor: dict[str, Any], license_id: str, *,
                      scope_area: str | None = None, valid_to: str | None = None,
                      status: str | None = None, occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        if not self.state.licenses.get(license_id):
            raise DomainError("授权不存在")
        current = self.state.current_license(license_id)
        payload = {"cultivar_version_id": current["cultivar_version_id"],
                   "scope_area": scope_area if scope_area is not None else current["scope_area"],
                   "valid_from": current["valid_from"],
                   "valid_to": valid_to if valid_to is not None else current["valid_to"],
                   "max_seedlings": current["max_seedlings"],
                   "status": status if status is not None else current["status"],
                   "licensor_id": current.get("licensor_id", actor["actor_id"])}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("LICENSE_SCOPE_AMENDED", license_id, payload, actor)

    # -- 繁育与交接 ------------------------------------------------------

    def create_nursery_batch(self, actor: dict[str, Any], nursery_batch_id: str,
                             cultivar_version_id: str, license_id: str,
                             produced_seedlings: int, *,
                             occurred_at: str | None = None) -> dict[str, Any]:
        if not self.state.licenses.get(license_id):
            raise DomainError("授权不存在，不能繁育")
        lic = self.state.current_license(license_id)
        if lic["cultivar_version_id"] != cultivar_version_id:
            raise DomainError("繁育批次品种与授权品种版本不符")
        if self.state.produced_per_license[license_id] + produced_seedlings > lic["max_seedlings"]:
            raise DomainError("累计繁育量将超过授权株数，违反数量守恒")
        if nursery_batch_id in self.state.nursery_batches:
            raise DomainError("繁育批次已存在")
        payload = {"nursery_id": actor["actor_id"], "cultivar_version_id": cultivar_version_id,
                   "license_id": license_id, "produced_seedlings": produced_seedlings}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("NURSERY_BATCH_CREATED", nursery_batch_id, payload, actor)

    def transfer_seedlings(self, actor: dict[str, Any], transfer_id: str,
                           nursery_batch_id: str, to_farmer_id: str, quantity: int,
                           *, occurred_at: str | None = None) -> dict[str, Any]:
        batch = self.state.nursery_batches.get(nursery_batch_id)
        if batch is None:
            raise DomainError("繁育批次不存在")
        if quantity <= 0:
            raise DomainError("交接数量必须为正")
        if self.state.seedling_out[nursery_batch_id] + quantity > batch["produced_seedlings"]:
            raise DomainError("累计出苗量超过批次产量，违反数量守恒")
        payload = {"nursery_batch_id": nursery_batch_id, "to_farmer_id": to_farmer_id,
                   "quantity": quantity, "cultivar_version_id": batch["cultivar_version_id"]}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("SEEDLING_TRANSFERRED", transfer_id, payload, actor)

    # -- 地块、农事、绿色核验、资格 --------------------------------------

    def plant_plot(self, actor: dict[str, Any], plot_id: str, farmer_id: str,
                   cultivar_version_id: str, seedling_count: int, *,
                   cooperative_id: str | None = None, license_id: str | None = None,
                   occurred_at: str | None = None) -> dict[str, Any]:
        if plot_id in self.state.plots:
            raise DomainError("地块已登记实种")
        if seedling_count <= 0:
            raise DomainError("实种株数必须为正")
        if seedling_count > self.state.seedlings_held[farmer_id]:
            raise DomainError(
                f"农户持有种苗 {self.state.seedlings_held[farmer_id]} 株，"
                f"实种 {seedling_count} 株，数量不守恒")
        payload = {"farmer_id": farmer_id, "cultivar_version_id": cultivar_version_id,
                   "seedling_count": seedling_count, "cooperative_id": cooperative_id,
                   "license_id": license_id}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("PLOT_PLANTED", plot_id, payload, actor)

    def record_farming(self, actor: dict[str, Any], record_id: str, plot_id: str,
                       technique: str, observed_at: str, *, evidence_ref: str = "") -> dict[str, Any]:
        if plot_id not in self.state.plots:
            raise DomainError("地块不存在")
        payload = {"record_id": record_id, "plot_id": plot_id, "technique": technique,
                   "observed_at": observed_at, "evidence_ref": evidence_ref}
        return self._append("FARMING_RECORDED", record_id, payload, actor)

    def verify_green_compliance(self, actor: dict[str, Any], verify_id: str, record_id: str,
                                plot_id: str, passed: bool, *, note: str = "") -> dict[str, Any]:
        self._check_separation(actor)
        payload = {"record_id": record_id, "plot_id": plot_id, "passed": passed,
                   "inspector_id": actor["actor_id"], "note": note}
        return self._append("GREEN_COMPLIANCE_VERIFIED", verify_id, payload, actor)

    def change_membership(self, actor: dict[str, Any], cooperative_id: str,
                          status: str, *, reason: str = "") -> dict[str, Any]:
        if status not in ("ACTIVE", "SUSPENDED", "WITHDRAWN"):
            raise DomainError("未知的合作社资格状态")
        payload = {"cooperative_id": cooperative_id, "status": status, "reason": reason}
        return self._append("MEMBERSHIP_CHANGED", f"coop-{cooperative_id}", payload, actor)

    # -- 鲜果批次 --------------------------------------------------------

    def harvest(self, actor: dict[str, Any], cherry_lot_id: str, plot_id: str,
                weight_g: int, receipt_id: str, *, occurred_at: str | None = None) -> dict[str, Any]:
        # 收购回执幂等：重送直接返回首次事件，不新增采收、不重复进入结算。
        existing = self.state.harvest_by_receipt.get(receipt_id)
        if existing is not None:
            return next(e for e in self.store.events if e["aggregate_id"] == existing)
        if plot_id not in self.state.plots:
            raise DomainError("地块尚未实种，不能采收")
        if weight_g <= 0:
            raise DomainError("采收重量必须为正")
        payload = {"plot_id": plot_id, "weight_g": weight_g, "receipt_id": receipt_id}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("CHERRY_HARVESTED", cherry_lot_id, payload, actor,
                            idempotency_key=f"receipt:{receipt_id}")

    def split_lot(self, actor: dict[str, Any], source_lot_id: str,
                  children: list[tuple[str, int]]) -> dict[str, Any]:
        node = self.state.cherry_nodes.get(source_lot_id)
        if node is None:
            raise DomainError("鲜果批次不存在")
        if source_lot_id in self.state.consumed_lots:
            raise DomainError("批次已被混批或入仓消耗，不能再拆")
        if not children:
            raise DomainError("至少拆出一个子批")
        total = sum(w for _, w in children)
        if any(w <= 0 for _, w in children):
            raise DomainError("子批重量必须为正")
        if total > node["weight_g"]:
            raise DomainError(f"拆出 {total} 克超过父批现有 {node['weight_g']} 克，数量不守恒")
        payload = {"source_lot_id": source_lot_id,
                   "children": [{"lot_id": lid, "weight_g": w} for lid, w in children]}
        return self._append("CHERRY_LOT_SPLIT", self._next_id("splitop"), payload, actor)

    def merge_lots(self, actor: dict[str, Any], merged_lot_id: str,
                   source_lot_ids: list[str]) -> dict[str, Any]:
        if merged_lot_id in self.state.cherry_nodes:
            raise DomainError("目标批次已存在")
        total = 0
        for src in source_lot_ids:
            node = self.state.cherry_nodes.get(src)
            if node is None:
                raise DomainError(f"来源批次 {src} 不存在")
            if src in self.state.consumed_lots:
                raise DomainError(f"来源批次 {src} 已消耗，不能重复混批")
            if node["weight_g"] <= 0:
                raise DomainError(f"来源批次 {src} 余量为 0")
            total += node["weight_g"]
        payload = {"source_lot_ids": list(source_lot_ids), "weight_g": total}
        return self._append("CHERRY_LOT_MERGED", merged_lot_id, payload, actor)

    def deliver(self, actor: dict[str, Any], delivery_id: str, cherry_lot_id: str,
                processor_id: str, sale_batch_id: str, weight_g: int,
                *, occurred_at: str | None = None) -> dict[str, Any]:
        node = self.state.cherry_nodes.get(cherry_lot_id)
        if node is None:
            raise DomainError("鲜果批次不存在")
        if cherry_lot_id in self.state.consumed_lots:
            raise DomainError("批次已消耗，不能重复入仓")
        if weight_g != node["weight_g"]:
            raise DomainError(
                f"入仓 {weight_g} 克与批次现有 {node['weight_g']} 克不一致，数量不守恒")
        if sale_batch_id in self.state.frozen:
            raise DomainError("销售批次已冻结，不能再追加交付")
        payload = {"cherry_lot_id": cherry_lot_id, "processor_id": processor_id,
                   "sale_batch_id": sale_batch_id, "weight_g": weight_g}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("CHERRY_DELIVERED", delivery_id, payload, actor)

    # -- 检验、复核、品种纠错、争议 --------------------------------------

    def grade(self, actor: dict[str, Any], grade_event_id: str, delivery_id: str,
              grade: str, weight_factor: float, *, note: str = "",
              event_type: str = "LOT_GRADED", occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        if delivery_id not in self.state.deliveries:
            raise DomainError("交付不存在，不能定级")
        if weight_factor < 0:
            raise DomainError("等级权重不能为负")
        payload = {"delivery_id": delivery_id, "grade": grade,
                   "weight_factor": weight_factor, "inspector_id": actor["actor_id"],
                   "note": note}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append(event_type, grade_event_id, payload, actor)

    def review_grade(self, actor: dict[str, Any], review_id: str, delivery_id: str,
                     grade: str, weight_factor: float, *, note: str = "",
                     occurred_at: str | None = None) -> dict[str, Any]:
        if not self.state.grades.get(delivery_id):
            raise DomainError("没有原等级可复核")
        return self.grade(actor, review_id, delivery_id, grade, weight_factor,
                          note=note, event_type="GRADE_REVIEWED", occurred_at=occurred_at)

    def correct_cultivar(self, actor: dict[str, Any], correction_id: str, plot_id: str,
                         cultivar_version_id: str, reason: str) -> dict[str, Any]:
        if plot_id not in self.state.plots:
            raise DomainError("地块不存在")
        if cultivar_version_id not in self.state.cultivar_versions:
            raise DomainError("目标品种版本未登记")
        if not reason.strip():
            raise DomainError("品种纠错必须填写理由")
        payload = {"plot_id": plot_id, "cultivar_version_id": cultivar_version_id,
                   "reason": reason}
        return self._append("CULTIVAR_CORRECTED", correction_id, payload, actor)

    def open_dispute(self, actor: dict[str, Any], dispute_id: str, ref_id: str,
                     reason: str, *, detail: str = "") -> dict[str, Any]:
        payload = {"ref_id": ref_id, "reason": reason, "detail": detail}
        return self._append("DISPUTE_OPENED", dispute_id, payload, actor)

    def resolve_dispute(self, actor: dict[str, Any], dispute_id: str, ref_id: str,
                        resolution: str, *, eligible: bool | None = None) -> dict[str, Any]:
        payload = {"ref_id": ref_id, "resolution": resolution, "eligible": eligible}
        return self._append("DISPUTE_RESOLVED", dispute_id, payload, actor)

    # -- 销售冻结与规则 --------------------------------------------------

    def freeze_premium(self, actor: dict[str, Any], sale_batch_id: str,
                       premium_fen: int, brand_ref: str, *,
                       occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        if sale_batch_id in self.state.frozen:
            raise DomainError("销售批次溢价已冻结")
        if premium_fen < 0:
            raise DomainError("冻结溢价不能为负")
        payload = {"premium_fen": premium_fen, "brand_ref": brand_ref}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("PREMIUM_FROZEN", sale_batch_id, payload, actor)

    def set_rules(self, actor: dict[str, Any], rules_id: str,
                  farmer_share: float, cooperative_share: float,
                  cultivar_royalty_share: float, *, note: str = "") -> dict[str, Any]:
        self._check_separation(actor)
        total = farmer_share + cooperative_share + cultivar_royalty_share
        if abs(total - 1.0) > 1e-9:
            raise DomainError(f"基础分配比例之和必须为 1，当前 {total}")
        payload = {"farmer_share": farmer_share, "cooperative_share": cooperative_share,
                   "cultivar_royalty_share": cultivar_royalty_share, "note": note}
        return self._append("ALLOCATION_RULES_SET", rules_id, payload, actor)

    # -- 合格性、依据、提案 ----------------------------------------------

    def _component_rows(self, delivery_id: str) -> list[dict[str, Any]]:
        delivery = self.state.deliveries[delivery_id]
        grade_history = self.state.grades.get(delivery_id, [])
        grade = grade_history[-1] if grade_history else None
        factor = grade["weight_factor"] if grade else 0.0
        rows = []
        for plot_id, qty in sorted(delivery["components"].items()):
            plot = self.state.plots[plot_id]
            ok, reasons = self.state.eligibility(plot_id, delivery)
            rows.append({
                "plot_id": plot_id, "farmer_id": plot["farmer_id"],
                "cooperative_id": plot.get("cooperative_id"),
                "cultivar_version_id": self.state.current_cultivar(plot_id),
                "license_id": plot.get("license_id"),
                "weight_g": qty,
                "weighted_g": round(qty * factor, 6),
                "eligible": ok, "reasons": reasons,
                "green_records": sorted(self.state.green_passed.get(plot_id, set())),
                "open_disputes": sorted(self.state.plot_disputes.get(plot_id, set())),
                "grade": grade["grade"] if grade else None,
            })
        return rows

    def _basis_for_delivery(self, delivery_id: str) -> dict[str, Any]:
        delivery = self.state.deliveries[delivery_id]
        history = self.state.grades.get(delivery_id, [])
        return {
            "delivery_id": delivery_id,
            "cherry_lot_id": delivery["cherry_lot_id"],
            "sale_batch_id": delivery["sale_batch_id"],
            "delivered_at": delivery["delivered_at"],
            "grade_event": history[-1] if history else None,
            "lineage": self.state.lineage(delivery["cherry_lot_id"]),
            "components": self._component_rows(delivery_id),
        }

    def _batch_weights(self, sale_batch_id: str) -> tuple[float, list[dict[str, Any]]]:
        """返回（批次全部加权克数、逐交付成分行）。等级一律取最新检验版本。"""
        total = 0.0
        rows: list[dict[str, Any]] = []
        for delivery_id in self.state.delivery_lots.get(sale_batch_id, []):
            basis = {"delivery_id": delivery_id, "components": self._component_rows(delivery_id)}
            rows.append(basis)
            total += sum(c["weighted_g"] for c in basis["components"])
        return total, rows

    def propose_allocation(self, actor: dict[str, Any], proposal_id: str,
                           sale_batch_id: str) -> dict[str, Any]:
        self._check_separation(actor)
        if sale_batch_id not in self.state.frozen:
            raise DomainError("销售批次尚未冻结溢价")
        if self.state.rules is None:
            raise DomainError("尚未设置分配规则")
        if proposal_id in self.state.proposals:
            raise DomainError("提案已存在")
        for delivery_id in self.state.delivery_lots.get(sale_batch_id, []):
            if not self.state.grades.get(delivery_id):
                raise DomainError(f"交付 {delivery_id} 尚未检验，不能提案")
        total_weight, rows = self._batch_weights(sale_batch_id)
        eligible_weight = sum(c["weighted_g"] for b in rows for c in b["components"]
                              if c["eligible"])
        payload = {"sale_batch_id": sale_batch_id,
                   "frozen_fen": self.state.frozen[sale_batch_id],
                   "rules_id": self.state.rules["rules_id"],
                   "total_weighted_g": total_weight,
                   "eligible_weighted_g": eligible_weight,
                   "delivery_bases": [self._basis_for_delivery(b["delivery_id"]) for b in rows]}
        return self._append("PREMIUM_ALLOCATION_PROPOSED", proposal_id, payload, actor)

    def _targets(self, sale_batch_id: str) -> dict[str, Any]:
        """按当前最新版本重算整个销售批次的目标分配（农户/合作社/品种权）。"""
        frozen = self.state.frozen[sale_batch_id]
        rules = self.state.rules
        total_weight, rows = self._batch_weights(sale_batch_id)
        eligible_components = [c for b in rows for c in b["components"] if c["eligible"]]
        eligible_weight = sum(c["weighted_g"] for c in eligible_components)
        eligible_portion = round(frozen * eligible_weight / total_weight) if total_weight else 0

        farmer_weights = {c["farmer_id"]: 0.0 for c in eligible_components}
        for c in eligible_components:
            farmer_weights[c["farmer_id"]] += c["weighted_g"]
        coop_weights: dict[str, float] = defaultdict(float)
        royalty_weights: dict[str, float] = defaultdict(float)
        for c in eligible_components:
            if c["cooperative_id"]:
                coop_weights[c["cooperative_id"]] += c["weighted_g"]
            royalty_weights[c["cultivar_version_id"]] += c["weighted_g"]

        farmer_pool = round(eligible_portion * rules["farmer_share"])
        coop_pool = round(eligible_portion * rules["cooperative_share"])
        royalty_pool = eligible_portion - farmer_pool - coop_pool  # 尾差归品种权
        return {
            "eligible_portion_fen": eligible_portion,
            "farmer": largest_remainder(farmer_pool, farmer_weights),
            "coop": largest_remainder(coop_pool, dict(coop_weights)),
            "royalty": largest_remainder(royalty_pool, dict(royalty_weights)),
            "rows": rows,
        }

    def post_settlement(self, actor: dict[str, Any], settlement_id: str,
                        proposal_id: str, *, occurred_at: str | None = None) -> dict[str, Any]:
        self._check_separation(actor)
        proposal = self.state.proposals.get(proposal_id)
        if proposal is None:
            raise DomainError("提案不存在")
        if proposal_id in self.state.posted_proposals:
            raise DomainError("提案已过账，重算差额请使用 post_adjustment")
        sale_batch_id = proposal["sale_batch_id"]
        targets = self._targets(sale_batch_id)
        amount = targets["eligible_portion_fen"]
        if amount <= 0:
            raise DomainError("当前没有合格成分，不能过账空结算；请先处理争议或等待资格恢复")
        if self.state.allocated[sale_batch_id] + amount > self.state.frozen[sale_batch_id]:
            raise DomainError("过账将超过冻结溢价，违反守恒")
        # 农户行附带完整依据指针，使每一分钱可回溯
        basis_by_farmer: dict[str, list[str]] = defaultdict(list)
        for basis in proposal["delivery_bases"]:
            for c in basis["components"]:
                if c["eligible"]:
                    basis_by_farmer[c["farmer_id"]].append(
                        f'{basis["delivery_id"]}/{c["plot_id"]}')
        lines = [{"farmer_id": fid, "amount_fen": amt,
                  "basis_refs": basis_by_farmer.get(fid, [])}
                 for fid, amt in sorted(targets["farmer"].items()) if amt]
        coop_lines = [{"cooperative_id": cid, "amount_fen": amt}
                      for cid, amt in sorted(targets["coop"].items()) if amt]
        royalty_lines = [{"cultivar_version_id": cv, "amount_fen": amt}
                         for cv, amt in sorted(targets["royalty"].items()) if amt]
        payload = {"proposal_id": proposal_id, "sale_batch_id": sale_batch_id,
                   "delivery_ids": [b["delivery_id"] for b in proposal["delivery_bases"]],
                   "premium_total_fen": amount,
                   "eligible_portion_fen": amount,
                   "lines": lines, "coop_lines": coop_lines, "royalty_lines": royalty_lines,
                   "basis": {"proposal_id": proposal_id,
                             "rules_id": proposal["rules_id"],
                             "brand_ref_premium_event": sale_batch_id}}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("SETTLEMENT_POSTED", settlement_id, payload, actor)

    def pay_settlement(self, actor: dict[str, Any], settlement_id: str,
                       payment_ref: str) -> dict[str, Any]:
        self._check_separation(actor)
        s = self.state.settlements.get(settlement_id)
        if s is None:
            raise DomainError("结算单不存在")
        if s["status"] == "PAID":
            raise DomainError("结算单已付款，不能重复登记付款")
        return self._append("SETTLEMENT_PAID", settlement_id,
                            {"payment_ref": payment_ref}, actor)

    # -- 差额调整（纠错/复核/资格/争议后按新版本重算）---------------------

    def post_adjustment(self, actor: dict[str, Any], adjustment_id: str,
                        sale_batch_id: str, reason: str,
                        *, occurred_at: str | None = None) -> dict[str, Any]:
        """按当前最新版本重算整个批次，与已入账净额求差。

        SETTLEMENT_POSTED / SETTLEMENT_PAID 原事实永不删除或改写；
        差额独立成单，可正可负，但：
        * 批次累计净分配不得超过冻结溢价；
        * 农户入账净额不得低于其已付款额（已付款不能被冲成负数）。
        """
        self._check_separation(actor)
        if not reason.strip():
            raise DomainError("差额调整必须给出理由")
        if sale_batch_id not in self.state.frozen:
            raise DomainError("销售批次不存在")
        if not self.state.settlements:
            raise DomainError("尚未有已过账结算，无需差额调整")
        targets = self._targets(sale_batch_id)
        farmer_lines, coop_lines, royalty_lines = [], [], []
        # 已消失的合格收款方也要冲回：目标字典缺省为 0
        farmer_ids = set(targets["farmer"]) | set(self.state.farmer_batch[sale_batch_id])
        for fid in sorted(farmer_ids):
            delta = targets["farmer"].get(fid, 0) \
                - self.state.farmer_batch[sale_batch_id].get(fid, 0)
            if delta:
                farmer_lines.append({"farmer_id": fid, "delta_fen": delta})
        coop_ids = set(targets["coop"]) | set(self.state.coop_batch[sale_batch_id])
        for cid in sorted(coop_ids):
            delta = targets["coop"].get(cid, 0) \
                - self.state.coop_batch[sale_batch_id].get(cid, 0)
            if delta:
                coop_lines.append({"cooperative_id": cid, "delta_fen": delta})
        royalty_ids = set(targets["royalty"]) | set(self.state.royalty_batch[sale_batch_id])
        for cv in sorted(royalty_ids):
            delta = targets["royalty"].get(cv, 0) \
                - self.state.royalty_batch[sale_batch_id].get(cv, 0)
            if delta:
                royalty_lines.append({"cultivar_version_id": cv, "delta_fen": delta})
        if not (farmer_lines or coop_lines or royalty_lines):
            raise DomainError("按新版本重算后无差额")
        net = sum(l["delta_fen"] for l in farmer_lines + coop_lines + royalty_lines)
        if self.state.allocated[sale_batch_id] + net > self.state.frozen[sale_batch_id]:
            raise DomainError("差额调整后累计净分配将超过冻结溢价，禁止过账")
        for line in farmer_lines:
            new_total = self.state.farmer_settled[line["farmer_id"]] + line["delta_fen"]
            if new_total < 0:
                raise DomainError(f"农户 {line['farmer_id']} 调整后入账净额为负")
            if new_total < self.state.farmer_paid[line["farmer_id"]]:
                raise DomainError(
                    f"农户 {line['farmer_id']} 负差额超过未付余额，"
                    "已付款事实不能被冲销，应先发起追款或争议")
        delivery_ids = [b["delivery_id"] for b in targets["rows"]]
        payload = {"sale_batch_id": sale_batch_id, "delivery_ids": delivery_ids,
                   "reason": reason, "lines": farmer_lines,
                   "coop_lines": coop_lines, "royalty_lines": royalty_lines,
                   "recomputed_with": {
                       "total_weighted_g": sum(
                           c["weighted_g"] for b in targets["rows"]
                           for c in b["components"]),
                       "eligible_weighted_g": sum(
                           c["weighted_g"] for b in targets["rows"]
                           for c in b["components"] if c["eligible"])}}
        if occurred_at:
            payload["_occurred_at"] = occurred_at
        return self._append("SETTLEMENT_ADJUSTMENT_POSTED", adjustment_id, payload, actor)

    # -- 人工调整 --------------------------------------------------------

    def manual_adjust(self, actor: dict[str, Any], adjustment_id: str, sale_batch_id: str,
                      lines: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        self._check_separation(actor)
        if not reason.strip():
            raise DomainError("人工调整必须填写理由")
        if sale_batch_id not in self.state.frozen:
            raise DomainError("销售批次不存在")
        clean = []
        for line in lines:
            delta = int(line.get("delta_fen", 0))
            if delta:
                clean.append({"farmer_id": line["farmer_id"],
                              "plot_id": line.get("plot_id"), "delta_fen": delta})
        if not clean:
            raise DomainError("人工调整没有有效行")
        net = sum(l["delta_fen"] for l in clean)
        if self.state.allocated[sale_batch_id] + net > self.state.frozen[sale_batch_id]:
            raise DomainError("人工调整后累计净分配将超过冻结销售收益，禁止过账")
        for line in clean:
            new_total = self.state.farmer_settled[line["farmer_id"]] + line["delta_fen"]
            if new_total < self.state.farmer_paid[line["farmer_id"]]:
                raise DomainError(
                    f"农户 {line['farmer_id']} 人工调整后净额低于已付款额，禁止过账")
        payload = {"sale_batch_id": sale_batch_id, "reason": reason, "lines": clean}
        return self._append("MANUAL_SHARE_ADJUSTED", adjustment_id, payload, actor)

    # -- 查询视图 --------------------------------------------------------

    def farmer_view(self, farmer_id: str) -> dict[str, Any]:
        """农户接口：只返回本人地块、本人交付成分与本人分配依据。"""
        plots = [plot for plot in self.state.plots.values()
                 if plot["farmer_id"] == farmer_id]
        my_deliveries = []
        for d_id, d in self.state.deliveries.items():
            mine = {pid: q for pid, q in d["components"].items()
                    if self.state.plots[pid]["farmer_id"] == farmer_id}
            if not mine:
                continue
            my_deliveries.append({
                "delivery_id": d_id, "sale_batch_id": d["sale_batch_id"],
                "delivered_at": d["delivered_at"], "my_components": mine,
                "grade_history": self.state.grades.get(d_id, []),
                "my_open_disputes": sorted({did for pid in mine
                                            for did in self.state.plot_disputes.get(pid, set())})})
        my_settlements = []
        for sid, s in self.state.settlements.items():
            for line in s["lines"]:
                if line["farmer_id"] == farmer_id:
                    my_settlements.append({
                        "settlement_id": sid, "status": s["status"],
                        "sale_batch_id": s["sale_batch_id"],
                        "amount_fen": line["amount_fen"],
                        "basis_refs": line.get("basis_refs", []),
                        "posted_at": s["posted_at"], "paid_at": s.get("paid_at")})
        my_adjustments = []
        for source in (self.state.adjustments, self.state.manual_adjustments):
            for a in source:
                lines = [l for l in a["lines"] if l["farmer_id"] == farmer_id]
                if lines:
                    my_adjustments.append({
                        "adjustment_id": a["adjustment_id"], "reason": a["reason"],
                        "sale_batch_id": a["sale_batch_id"], "lines": lines})
        return {
            "farmer_id": farmer_id, "plots": plots, "deliveries": my_deliveries,
            "settlements": my_settlements, "adjustments": my_adjustments,
            "settled_total_fen": self.state.farmer_settled[farmer_id],
            "paid_total_fen": self.state.farmer_paid[farmer_id]}

    def premium_trace(self, sale_batch_id: str, farmer_id: str | None = None) -> dict[str, Any]:
        """证明一笔品牌溢价如何穿过批次谱系到达农户。"""
        if sale_batch_id not in self.state.frozen:
            raise DomainError("销售批次不存在")
        bases = [self._basis_for_delivery(d)
                 for d in self.state.delivery_lots.get(sale_batch_id, [])]
        received = []
        for sid, s in self.state.settlements.items():
            if s["sale_batch_id"] != sale_batch_id:
                continue
            for line in s["lines"]:
                if farmer_id is not None and line["farmer_id"] != farmer_id:
                    continue
                received.append({"settlement_id": sid, "status": s["status"],
                                 "posted_at": s["posted_at"], "paid_at": s.get("paid_at"),
                                 **line})
        farmer_net = defaultdict(int)
        for source in (self.state.adjustments, self.state.manual_adjustments):
            for a in source:
                if a["sale_batch_id"] != sale_batch_id:
                    continue
                for line in a["lines"]:
                    if farmer_id is not None and line["farmer_id"] != farmer_id:
                        continue
                    farmer_net[line["farmer_id"]] += line.get("delta_fen", 0)
        allocated = self.state.allocated[sale_batch_id]
        frozen = self.state.frozen[sale_batch_id]
        return {
            "sale_batch_id": sale_batch_id,
            "frozen_premium_fen": frozen,
            "allocated_net_fen": allocated,
            "frozen_remaining_fen": frozen - allocated,
            "lineage": self.state.lineage(sale_batch_id),
            "deliveries": bases,
            "received": received,
            "adjustment_net_by_farmer": dict(farmer_net),
            "conservation_ok": allocated <= frozen}

    def pending_work(self) -> dict[str, Any]:
        """中断恢复后重建：待结算交付、未决争议、已过账待付款、冻结池余额。"""
        pending_settlement = []
        for d_id, d in self.state.deliveries.items():
            rows = self._component_rows(d_id)
            pending_settlement.append({
                "delivery_id": d_id, "sale_batch_id": d["sale_batch_id"],
                "already_posted": d_id in self.state.posted_deliveries,
                "components": [{"plot_id": c["plot_id"], "farmer_id": c["farmer_id"],
                                "eligible": c["eligible"], "reasons": c["reasons"]}
                               for c in rows]})
        open_disputes: dict[str, list[str]] = defaultdict(list)
        for ref_id, ids in self.state.lot_disputes.items():
            for did in ids:
                open_disputes[ref_id].append(did)
        for plot_id, ids in self.state.plot_disputes.items():
            for did in ids:
                if did not in open_disputes[plot_id]:
                    open_disputes[plot_id].append(did)
        frozen_status = [
            {"sale_batch_id": sid, "frozen_fen": self.state.frozen[sid],
             "allocated_net_fen": self.state.allocated[sid],
             "remaining_fen": self.state.frozen[sid] - self.state.allocated[sid]}
            for sid in self.state.frozen]
        return {
            "pending_settlement": pending_settlement,
            "open_disputes": dict(open_disputes),
            "unpaid_posted_settlements": [
                sid for sid, s in self.state.settlements.items() if s["status"] == "POSTED"],
            "proposals": list(self.state.proposals),
            "frozen_status": frozen_status}
