"""领域常量、金额与计量工具。

本模块不保存业务状态，只提供全服务共用的领域词汇：
主体角色、事件名称、计量单位，以及金额/数量的定点运算。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any

# ---- 主体身份角色（一个主体可在业务中扮演多个经济角色） ----

PARTY_FARMER = "FARMER"          # 农户
PARTY_NURSERY = "NURSERY"        # 苗圃
PARTY_BREEDER = "BREEDER"        # 育种单位
PARTY_COOP = "COOPERATIVE"       # 合作社
PARTY_MILL = "MILL"              # 加工厂
PARTY_BRAND = "BRAND"            # 品牌/采购方
PARTY_ALLIANCE = "ALLIANCE"      # 产业联盟（运营方）
PARTY_ROLES = frozenset({
    PARTY_FARMER, PARTY_NURSERY, PARTY_BREEDER,
    PARTY_COOP, PARTY_MILL, PARTY_BRAND, PARTY_ALLIANCE,
})

# ---- 职务（签名职务），三类职务相互排斥，不能由同一主体同时担任 ----

DUTY_LICENSOR = "LICENSOR"       # 良种授权人
DUTY_INSPECTOR = "INSPECTOR"     # 检验员
DUTY_SETTLER = "SETTLER"         # 结算人
DUTY_ROLES = frozenset({DUTY_LICENSOR, DUTY_INSPECTOR, DUTY_SETTLER})

# 事件类型 → 必须由哪一类职务主体签名
SIGNING_DUTY: dict[str, str] = {
    "LICENSE_GRANTED": DUTY_LICENSOR,
    "LICENSE_AMENDED": DUTY_LICENSOR,
    "LOT_GRADED": DUTY_INSPECTOR,
    "GRADE_REVISED": DUTY_INSPECTOR,
    "CULTIVAR_CORRECTED": DUTY_LICENSOR,
    "PREMIUM_FROZEN": DUTY_SETTLER,
    "SETTLEMENT_RULE_PUBLISHED": DUTY_SETTLER,
    "SETTLEMENT_POSTED": DUTY_SETTLER,
    "SETTLEMENT_REVISED": DUTY_SETTLER,
    "SETTLEMENT_ADJUSTED": DUTY_SETTLER,
    "SETTLEMENT_PAID": DUTY_SETTLER,
}

# ---- 计量单位 ----

UNIT_SEEDLING = "SEEDLING"       # 株
UNIT_KG = "KG"                   # 千克（鲜果）
UNITS = frozenset({UNIT_SEEDLING, UNIT_KG})

# ---- 检验结论 ----

GRADE_SPECIAL = "SPECIAL"        # 优品
GRADE_GRADE_A = "GRADE_A"
GRADE_REJECT = "REJECT"
GRADES = frozenset({GRADE_SPECIAL, GRADE_GRADE_A, GRADE_REJECT})

# ---- 争议状态 ----

DISPUTE_OPEN = "OPEN"
DISPUTE_UPHELD = "UPHELD"        # 支持申诉
DISPUTE_REJECTED = "REJECTED"    # 驳回
DISPUTE_OUTCOMES = frozenset({DISPUTE_UPHELD, DISPUTE_REJECTED})

MONEY_QUANT = Decimal("0.01")    # 金额精确到分
QTY_QUANT = Decimal("0.001")     # 数量精确到 0.001


def D(value: Any) -> Decimal:
    """把 JSON 里的数字/字符串安全转成定点 Decimal（拒绝 float 漂移）。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为金额或数量")
    if isinstance(value, float):
        raise ValueError("金额/数量禁止使用 float，改用字符串或整数")
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    """金额归一到分。"""
    return D(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    """数量归一到 0.001。"""
    return D(value).quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


class FlowKind(str, Enum):
    """谱系边的语义：上一批次数量以何种方式进入下一批次。"""

    SPLIT = "SPLIT"          # 拆分：一批 → 多批
    MERGE = "MERGE"          # 混批：多批 → 一批
    TRANSFORM = "TRANSFORM"  # 加工：鲜果 → 处理批次（允许出成率折算）
    DELIVER = "DELIVER"      # 农户交付：地块鲜果 → 收购批次
