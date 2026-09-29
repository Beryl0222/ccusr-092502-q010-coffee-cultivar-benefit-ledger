"""批次谱系与数量守恒。

用一张有向图记录"数量从哪里来、到哪里去"：

    地块 --交付--> 收购批次 --混批/拆分/加工--> 销售批次

边只追加、不修改；拆分 (SPLIT)、混批 (MERGE) 严格守恒，
加工 (TRANSFORM) 允许出成率损耗但不允许产出大于投入。
任意批次可向上还原到收购回执与地块，也可向下定位到销售批次，
并按质量比例把销售批次的千克数摊回每张收购回执。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .domain import UNIT_KG, FlowKind, qty

MIXED_CULTIVAR = "MIXED"  # 混批来源品种不一致时的混合标识


class LineageError(ValueError):
    """谱系或数量守恒被违反。"""


@dataclass(frozen=True)
class FlowEdge:
    kind: FlowKind
    sources: tuple[tuple[str, Decimal], ...]   # (节点 id, 千克)
    sinks: tuple[tuple[str, Decimal], ...]
    meta: dict = field(default_factory=dict)


class Genealogy:
    def __init__(self) -> None:
        # lot_id -> {"unit", "remaining"}，批次余量台账
        self._lots: dict[str, dict] = {}
        # lot_id -> 诞生素的边（交付进入或混批/拆分/加工生成）
        self._inbound: dict[str, list[FlowEdge]] = {}
        # lot_id -> 支取该批次的边（拆分/混批/加工）
        self._outbound: dict[str, list[FlowEdge]] = {}
        # plot_id -> 交付边
        self._deliveries: dict[str, list[FlowEdge]] = {}
        # 批次品种底账：交付时按地块品种盖戳
        self.lot_cultivar: dict[str, str] = {}       # lot_id -> 品种 id
        self.lot_cultivar_version: dict[str, str] = {}
        # 品种纠错盖戳（覆盖有效品种，但不抹掉历史戳记）
        self.lot_correction: dict[str, str] = {}          # lot_id -> 新版本
        self.lot_correction_cultivar: dict[str, str] = {} # lot_id -> 品种 id

    # ---- 登记 ----

    def register_lot(self, lot_id: str, unit: str, quantity: Decimal) -> None:
        if lot_id in self._lots:
            raise LineageError(f"批次已存在: {lot_id}")
        self._lots[lot_id] = {"unit": unit, "remaining": qty(quantity), "born": qty(quantity)}

    def has_lot(self, lot_id: str) -> bool:
        return lot_id in self._lots

    def remaining(self, lot_id: str) -> Decimal:
        return self._lots[lot_id]["remaining"]

    def stamp_cultivar(self, lot_id: str, cultivar_id: str, cultivar_version: str) -> None:
        self.lot_cultivar[lot_id] = cultivar_id
        self.lot_cultivar_version[lot_id] = cultivar_version

    def correct_cultivar(self, lot_id: str, cultivar_id: str, cultivar_version: str) -> None:
        """品种纠错：盖新戳于源头批次，下游沿谱系向上解析时自动取得新版本。"""
        if lot_id not in self._lots:
            raise LineageError(f"未知批次: {lot_id}")
        self.lot_correction[lot_id] = cultivar_version
        self.lot_correction_cultivar[lot_id] = cultivar_id

    # ---- 流转 ----

    def validate_flow(self, kind: FlowKind,
                      sources: list[tuple[str, Decimal]],
                      sinks: list[tuple[str, Decimal]],
                      allow_loss: bool = False) -> None:
        """无副作用的守恒预校验（命令层落盘前调用），规则与 flow 完全一致。"""
        if kind is FlowKind.DELIVER:
            raise LineageError("交付请使用 deliver")
        if not sources or not sinks:
            raise LineageError("流转必须同时有来源与产出")
        src = [(lid, qty(v)) for lid, v in sources]
        snk = [(lid, qty(v)) for lid, v in sinks]
        for lid, value in src:
            if lid not in self._lots:
                raise LineageError(f"未知来源批次: {lid}")
            if self._lots[lid]["unit"] != UNIT_KG:
                raise LineageError(f"批次 {lid} 不是鲜果/产品千克批次")
            if value <= 0:
                raise LineageError("流转数量必须为正")
        for lid, value in snk:
            if lid in self._lots:
                raise LineageError(f"产出批次 {lid} 已存在，不能重复诞生")
            if value <= 0:
                raise LineageError("流转数量必须为正")
        total_in = sum((v for _, v in src), Decimal(0))
        total_out = sum((v for _, v in snk), Decimal(0))
        for lid, value in src:
            if value > self._lots[lid]["remaining"]:
                raise LineageError(
                    f"批次 {lid} 支取 {value} 超过余量 {self._lots[lid]['remaining']}"
                )
        if allow_loss:
            if total_out > total_in:
                raise LineageError(
                    f"加工产出 {total_out} 大于投入 {total_in}，数量不守恒"
                )
        elif total_out != total_in:
            raise LineageError(
                f"{kind.value} 数量不守恒: 投入 {total_in} ≠ 产出 {total_out}"
            )

    def deliver(self, plot_id: str, lot_id: str, quantity_kg: Decimal,
                receipt_no: str, cultivar_id: str, cultivar_version: str) -> FlowEdge:
        """地块鲜果交付进入收购批次（收购批次在首次交付时自动开批）。"""
        amount = qty(quantity_kg)
        if amount <= 0:
            raise LineageError("交付数量必须为正")
        if lot_id not in self._lots:
            self.register_lot(lot_id, UNIT_KG, Decimal(0))
        lot = self._lots[lot_id]
        if lot["unit"] != UNIT_KG:
            raise LineageError("鲜果只能交付到千克计量单位的批次")
        # 收购批次只能由原始交付汇集；混批/加工产物不得再直接收鲜果
        if any(e.kind is not FlowKind.DELIVER for e in self._inbound.get(lot_id, [])):
            raise LineageError("加工产物批次不得直接收购鲜果")
        edge = FlowEdge(
            kind=FlowKind.DELIVER,
            sources=((plot_id, amount),),
            sinks=((lot_id, amount),),
            meta={"receipt_no": receipt_no},
        )
        lot["remaining"] = qty(lot["remaining"] + amount)
        lot["born"] = qty(lot["born"] + amount)
        self.lot_cultivar.setdefault(lot_id, cultivar_id)
        self.lot_cultivar_version.setdefault(lot_id, cultivar_version)
        self._inbound.setdefault(lot_id, []).append(edge)
        self._deliveries.setdefault(plot_id, []).append(edge)
        return edge

    def flow(self, kind: FlowKind,
             sources: list[tuple[str, Decimal]],
             sinks: list[tuple[str, Decimal]],
             allow_loss: bool = False,
             meta: dict | None = None) -> FlowEdge:
        """登记一条拆分/混批/加工边并执行守恒校验。

        来源批次必须已存在且余量充足；产出批次必须是全新批次，
        在此刻按产出量诞生（一批只有一种诞生方式，保证谱系可逆）。
        """
        if kind is FlowKind.DELIVER:
            raise LineageError("交付请使用 deliver")
        if not sources or not sinks:
            raise LineageError("流转必须同时有来源与产出")
        src = [(lid, qty(v)) for lid, v in sources]
        snk = [(lid, qty(v)) for lid, v in sinks]
        self.validate_flow(kind, src, snk, allow_loss=allow_loss)
        edge = FlowEdge(kind=kind, sources=tuple(src), sinks=tuple(snk), meta=meta or {})
        for lid, value in src:
            self._lots[lid]["remaining"] -= value
        for lid, value in snk:
            self.register_lot(lid, UNIT_KG, value)
            self._inbound.setdefault(lid, []).append(edge)
        for lid, _ in src:
            self._outbound.setdefault(lid, []).append(edge)
        self._inherit_cultivar(edge)
        return edge

    def _inherit_cultivar(self, edge: FlowEdge) -> None:
        """诞生时记录静态底戳；有效品种以 effective_cultivar 动态向上解析为准，
        这样源头批次的纠错能自动传播到全部下游。"""
        resolved = {self.effective_cultivar(lid) for lid, _ in edge.sources}
        if len(resolved) == 1:
            cid, ver = next(iter(resolved))
            for lid, _ in edge.sinks:
                self.lot_cultivar[lid] = cid
                self.lot_cultivar_version[lid] = ver
        else:
            for lid, _ in edge.sinks:
                self.lot_cultivar[lid] = MIXED_CULTIVAR
                self.lot_cultivar_version[lid] = MIXED_CULTIVAR

    # ---- 查询：可逆谱系 ----

    def effective_cultivar(self, lot_id: str) -> tuple[str, str]:
        """返回有效 (品种 id, 版本)：本批纠错盖戳优先；流转产物沿诞生边向上解析，
        全部来源一致则继承，不一致为 MIXED；收购批次用交付底戳。"""
        if lot_id in self.lot_correction:
            return self.lot_correction_cultivar[lot_id], self.lot_correction[lot_id]
        inbound = self._inbound.get(lot_id, [])
        births = [e for e in inbound if e.kind is not FlowKind.DELIVER]
        if births:
            edge = births[0]
            resolved = {self.effective_cultivar(lid) for lid, _ in edge.sources}
            if len(resolved) == 1:
                return next(iter(resolved))
            return MIXED_CULTIVAR, MIXED_CULTIVAR
        return (self.lot_cultivar.get(lot_id, MIXED_CULTIVAR),
                self.lot_cultivar_version.get(lot_id, MIXED_CULTIVAR))

    def upward(self, lot_id: str) -> list[str]:
        """向上谱系：产出批次 → 来源批次 → … → 地块（去重保序）。"""
        seen: set[str] = set()
        order: list[str] = []

        def walk(node: str) -> None:
            for edge in self._inbound.get(node, []):
                for src, _ in edge.sources:
                    if src not in seen:
                        seen.add(src)
                        order.append(src)
                        walk(src)

        walk(lot_id)
        return order

    def downward(self, node_id: str) -> list[str]:
        """向下谱系：来源批次/地块 → 所有流经的下游批次。"""
        seen: set[str] = set()
        order: list[str] = []

        def walk(node: str) -> None:
            edges = list(self._outbound.get(node, [])) + list(self._deliveries.get(node, []))
            for edge in edges:
                for snk, _ in edge.sinks:
                    if snk not in seen:
                        seen.add(snk)
                        order.append(snk)
                        walk(snk)

        walk(node_id)
        return order

    def receipts(self, lot_id: str) -> dict[str, Decimal]:
        """把某批次诞生时的全部千克摊回 {收购回执号: 千克}。

        先求每千克构成（份额合计为 1），再乘批次诞生量：
        - 纯收购批次：按各次交付量占比；
        - 流转产物：来源每千克构成按来源投入占比加权。
        加工损耗按比例自动落在各回执上。
        """
        born = self._lots[lot_id]["born"]
        return {receipt: qty(share * born)
                for receipt, share in self.composition(lot_id).items()
                if qty(share * born) > 0}

    def composition(self, lot_id: str) -> dict[str, Decimal]:
        """每千克该批次来自哪些收购回执（份额，合计为 1）。"""
        inbound = self._inbound.get(lot_id, [])
        if not inbound:
            return {}
        deliveries = [e for e in inbound if e.kind is FlowKind.DELIVER]
        births = [e for e in inbound if e.kind is not FlowKind.DELIVER]
        if deliveries:
            total = sum((e.sources[0][1] for e in deliveries), Decimal(0))
            acc: dict[str, Decimal] = {}
            for edge in deliveries:
                value = edge.sources[0][1]
                acc[edge.meta["receipt_no"]] = acc.get(edge.meta["receipt_no"], Decimal(0)) + value / total
            return acc
        edge = births[0]  # 每批只有唯一诞生边
        total_in = sum((v for _, v in edge.sources), Decimal(0))
        acc = {}
        for src, src_qty in edge.sources:
            weight = src_qty / total_in
            for receipt, share in self.composition(src).items():
                acc[receipt] = acc.get(receipt, Decimal(0)) + weight * share
        return acc

    def receipt_to_lot(self, receipt_no: str) -> str | None:
        """收购回执所在的收购批次。"""
        for lot_id, edges in self._inbound.items():
            for edge in edges:
                if edge.kind is FlowKind.DELIVER and edge.meta.get("receipt_no") == receipt_no:
                    return lot_id
        return None

    def deliveries_of_plot(self, plot_id: str) -> list[FlowEdge]:
        return list(self._deliveries.get(plot_id, []))

    def all_receipts(self) -> dict[str, str]:
        """{回执号: 收购批次}。"""
        out: dict[str, str] = {}
        for lot_id, edges in self._inbound.items():
            for edge in edges:
                if edge.kind is FlowKind.DELIVER:
                    out[edge.meta["receipt_no"]] = lot_id
        return out

    def describe_edge(self, edge: FlowEdge) -> dict:
        return {
            "kind": edge.kind.value,
            "sources": [{"lot": lid, "quantity_kg": str(v)} for lid, v in edge.sources],
            "sinks": [{"lot": lid, "quantity_kg": str(v)} for lid, v in edge.sinks],
            "meta": edge.meta,
        }
