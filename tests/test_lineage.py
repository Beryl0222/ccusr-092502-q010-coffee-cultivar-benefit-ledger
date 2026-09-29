"""谱系与数量守恒测试。"""
from __future__ import annotations

import unittest
from decimal import Decimal

from src.domain import FlowKind
from src.lineage import Genealogy, LineageError


class ConservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.g = Genealogy()
        self.g.deliver("plot-a", "buy-1", "100", "R-1", "C-1", "v1")
        self.g.deliver("plot-b", "buy-1", "300", "R-2", "C-1", "v1")

    def test_split_must_conserve(self) -> None:
        with self.assertRaises(LineageError):
            self.g.flow(FlowKind.SPLIT,
                        [("buy-1", "400")], [("x", "300"), ("y", "101")])

    def test_withdraw_cannot_exceed_remaining(self) -> None:
        self.g.flow(FlowKind.SPLIT, [("buy-1", "300")], [("x", "300")])
        with self.assertRaises(LineageError):
            self.g.flow(FlowKind.SPLIT, [("buy-1", "101")], [("y", "101")])

    def test_transform_allow_loss_but_not_gain(self) -> None:
        self.g.flow(FlowKind.TRANSFORM, [("buy-1", "400")], [("g", "320")],
                    allow_loss=True)
        g2 = Genealogy()
        g2.deliver("p", "b", "100", "R", "C-1", "v1")
        with self.assertRaises(LineageError):
            g2.flow(FlowKind.TRANSFORM, [("b", "100")], [("g", "101")],
                    allow_loss=True)

    def test_sink_cannot_be_born_twice(self) -> None:
        self.g.flow(FlowKind.SPLIT, [("buy-1", "400")], [("x", "400")])
        with self.assertRaises(LineageError):
            self.g.flow(FlowKind.SPLIT, [("x", "400")], [("buy-1", "400")])


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.g = Genealogy()
        self.g.deliver("plot-a", "buy-1", "100", "R-1", "C-1", "v1")
        self.g.deliver("plot-b", "buy-1", "300", "R-2", "C-1", "v1")
        self.g.flow(FlowKind.MERGE, [("buy-1", "400")], [("washed", "400")])
        self.g.flow(FlowKind.TRANSFORM, [("washed", "400")], [("green", "320")],
                    allow_loss=True)
        self.g.flow(FlowKind.SPLIT, [("green", "320")],
                    [("sale", "200"), ("tail", "120")])

    def test_receipt_share_is_mass_proportional(self) -> None:
        self.assertEqual(self.g.receipts("sale"),
                         {"R-1": Decimal("50.000"), "R-2": Decimal("150.000")})
        self.assertEqual(self.g.receipts("tail"),
                         {"R-1": Decimal("30.000"), "R-2": Decimal("90.000")})

    def test_reversible_paths(self) -> None:
        self.assertEqual(self.g.upward("sale"),
                         ["green", "washed", "buy-1", "plot-a", "plot-b"])
        downstream = self.g.downward("plot-a")
        self.assertEqual(downstream,
                         ["buy-1", "washed", "green", "sale", "tail"])

    def test_cultivar_correction_propagates_downstream(self) -> None:
        self.assertEqual(self.g.effective_cultivar("sale"), ("C-1", "v1"))
        self.g.correct_cultivar("buy-1", "C-1", "v2")
        self.assertEqual(self.g.effective_cultivar("sale"), ("C-1", "v2"))
        self.assertEqual(self.g.effective_cultivar("tail"), ("C-1", "v2"))

    def test_mixed_origin_marked(self) -> None:
        g = Genealogy()
        g.deliver("p1", "b1", "50", "R-1", "C-1", "v1")
        g.deliver("p2", "b2", "50", "R-2", "C-2", "v1")
        g.flow(FlowKind.MERGE, [("b1", "50"), ("b2", "50")], [("mix", "100")])
        self.assertEqual(g.effective_cultivar("mix"),
                         ("MIXED", "MIXED"))
        g.correct_cultivar("b1", "C-2", "v1")
        self.assertEqual(g.effective_cultivar("mix"), ("C-2", "v1"))


if __name__ == "__main__":
    unittest.main()
