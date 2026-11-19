"""业务规则测试：意见认定、映射、偏好证据、可行组合与组合比较。"""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from council.core import (
    _best_relation,
    active_commitments,
    all_portfolio_relations,
    build_opinion_ledger,
    build_pair_preferences,
    build_project_mapping,
    compare_portfolios,
    dominated_portfolios,
    enumerate_feasible_portfolios,
    pair_relation,
    parse_dt,
)
from council.workbench import build_report, public_report, run_workspace
from inspect_inputs import load_tables

DATA = Path(__file__).resolve().parents[1] / "data"
START = parse_dt("2026-04-01T08:00:00+08:00")
DEADLINE = parse_dt("2026-04-12T18:00:00+08:00")
WEIGHTS = {"north": 3, "south": 2}


def _setup():
    tables = load_tables(DATA)
    mapping = build_project_mapping(tables["projects"])
    ledger = build_opinion_ledger(
        tables["residents"], tables["ballots"], mapping, WEIGHTS, START, DEADLINE
    )
    return tables, mapping, ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.mapping, self.ledger = _setup()

    def test_eligible_residents_keep_full_weight_without_redistribution(self):
        # 5 名资格居民：north 三人各 3、south 两人各 2，合计 13；r5 无资格。
        self.assertEqual(self.ledger.eligible_weight, 13)
        self.assertEqual(self.ledger.total_weight, 13)
        self.assertEqual(len(self.ledger.opinions), 5)
        by_resident = {o.resident: o for o in self.ledger.opinions}
        self.assertEqual([by_resident[r].weight for r in ("r1", "r2", "r6")], [3, 3, 3])
        self.assertEqual([by_resident[r].weight for r in ("r3", "r4")], [2, 2])

    def test_confirmed_revision_replaces_whole_ballot(self):
        # r2 的最后确认件 b8 整票替换 b2，不拼接两份排序。
        r2 = next(o for o in self.ledger.opinions if o.resident == "r2")
        self.assertEqual(r2.source_receipts, ("b8",))
        flat = [item for tier in r2.ranking for item in tier]
        self.assertEqual(flat, ["mark", "bench", "tree", "path", "play"])

    def test_transcribed_ballot_does_not_override(self):
        receipts = {item.receipt: item for item in self.ledger.excluded}
        self.assertEqual(receipts["b6"].reason, "代录件未经本人确认，不覆盖原意见")
        r2 = next(o for o in self.ledger.opinions if o.resident == "r2")
        self.assertNotIn("b6", r2.source_receipts)

    def test_late_and_ineligible_ballots_are_excluded(self):
        receipts = {item.receipt: item for item in self.ledger.excluded}
        self.assertIn("b5", receipts)  # r5 eligible=no
        self.assertIn("b9", receipts)  # 2026-04-12 19:00 晚于截止时间
        r3 = next(o for o in self.ledger.opinions if o.resident == "r3")
        self.assertEqual(r3.source_receipts, ("b3",))

    def test_rename_inherits_rank_but_merge_does_not(self):
        r1 = next(o for o in self.ledger.opinions if o.resident == "r1")
        # seat 改名为 bench，承接 r1 的旧顺位；ramp/light 合并不折入 path。
        self.assertEqual(r1.ranking, (("bench",),))
        self.assertNotIn("path", {item for tier in r1.ranking for item in tier})
        notes = " ".join(r1.notes)
        self.assertIn("改名承接", notes)
        self.assertIn("实质合并不继承整包支持", notes)
        # 旧票明确比较过的内容仍留痕，可追溯但不约束合并方案。
        legacy = {(a, b): rel for a, b, rel in r1.legacy_edges}
        self.assertEqual(legacy[("ramp", "light")], "left")
        self.assertEqual(legacy[("ramp", "seat")], "tie")

    def test_legacy_ballot_cannot_support_merge_target(self):
        r1 = next(o for o in self.ledger.opinions if o.resident == "r1")
        for other in ("bench", "play", "mark", "tree"):
            relation, reason = pair_relation(r1, "path", other, self.mapping)
            self.assertEqual(relation, "unknown")
            self.assertIn("口径不同", reason)
        # r1 对 bench 的比较同样只能是未知：没有明确反对不等于支持。
        for other in ("play", "mark", "tree"):
            relation, _ = pair_relation(r1, "bench", other, self.mapping)
            self.assertEqual(relation, "unknown")


class PairPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.mapping, self.ledger = _setup()
        self.pairs = {
            (p.left, p.right): p for p in build_pair_preferences(self.ledger, self.mapping)
        }

    def test_weights_sum_to_total(self):
        for pair in self.pairs.values():
            self.assertEqual(
                pair.left_weight + pair.right_weight + pair.tie_weight + pair.unknown_weight,
                13,
            )

    def test_path_vs_bench_keeps_merge_incomparable_weight(self):
        pair = self.pairs[("path", "bench")]
        # r1(3) 口径不同；r2/r6 明确 bench>path 共 6；r3/r4 明确标注未比较共 4。
        self.assertEqual((pair.left_weight, pair.right_weight, pair.tie_weight), (0, 6, 0))
        self.assertEqual(pair.unknown_weight, 7)
        self.assertEqual(pair.status, "contestable")
        self.assertEqual(
            pair.unknown_reasons["旧册与合并方案口径不同，旧表未直接比较"], 3
        )

    def test_robust_pair_survives_worst_case_completion(self):
        # mark>path：r2、r4、r6 共 8；即便 5 张未知全部补成 path>mark 仍领先。
        pair = self.pairs[("path", "mark")]
        self.assertEqual((pair.left_weight, pair.right_weight), (0, 8))
        self.assertEqual(pair.status, "robust_loss")

    def test_contestable_pair_can_flip_without_violating_ballots(self):
        # path>play：r6/r4 共 3（path 优于 play），r3 反向 2，8 张未知可翻转。
        pair = self.pairs[("path", "play")]
        self.assertEqual((pair.left_weight, pair.right_weight), (3, 2))
        self.assertEqual(pair.unknown_weight, 8)
        self.assertEqual(pair.status, "contestable")

    def test_explicit_unknown_is_not_counted_as_support(self):
        # r3 把 path/tree 标为未比较；任何成对统计中该 2 权重必须落入未知。
        pair = self.pairs[("path", "tree")]
        self.assertEqual(pair.right_weight, 8)
        self.assertEqual(pair.unknown_weight, 5)
        self.assertEqual(pair.status, "robust_loss")

    def test_ties_are_neutral_and_keep_pair_contestable(self):
        # bench vs mark：r2 同档(3)，r6/r3 明确 bench>mark 共 5，r1/r4 未知共 5。
        pair = self.pairs[("bench", "mark")]
        self.assertEqual(pair.tie_weight, 3)
        self.assertEqual((pair.left_weight, pair.right_weight), (5, 0))
        self.assertEqual(pair.status, "contestable")


class FeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.mapping, self.ledger = _setup()
        self.result = enumerate_feasible_portfolios(
            self.tables["projects"],
            self.tables["commitments"],
            budget=110,
            min_access=1,
        )

    def test_commitment_lifecycle(self):
        active = active_commitments(self.tables["commitments"], self.tables["projects"])
        self.assertEqual(active, ("bench",))  # tree 的 d2 已撤回
        self.assertEqual(self.result.committed, ("bench",))
        self.assertEqual(self.result.committed_cost, 20)
        self.assertEqual(self.result.remaining_budget, 90)

    def test_every_feasible_portfolio_includes_commitment_and_rules(self):
        codes = {p.code: p.projects for p in self.result.portfolios}
        for projects in codes.values():
            self.assertIn("bench", projects)
        self.assertEqual(
            set(codes.values()),
            {
                ("bench", "mark"),
                ("bench", "path"),
                ("bench", "mark", "path"),
                ("bench", "mark", "play"),
                ("bench", "mark", "tree"),
            },
        )

    def test_path_play_mutual_exclusion_is_recorded(self):
        clash = next(
            item
            for item in self.result.infeasible
            if set(item.projects) == {"bench", "path", "play"}
        )
        self.assertTrue(any("不可同建" in reason for reason in clash.reasons))

    def test_access_floor_is_enforced(self):
        no_access = next(
            item
            for item in self.result.infeasible
            if set(item.projects) == {"bench", "play"}
        )
        self.assertTrue(any("无障碍" in reason for reason in no_access.reasons))

    def test_budget_cap_is_enforced(self):
        over = next(
            item
            for item in self.result.infeasible
            if set(item.projects) == {"bench", "path", "play", "mark"}
        )
        self.assertTrue(any("费用" in reason for reason in over.reasons))

    def test_requires_dependency_is_enforced(self):
        # mark 依赖 bench；去掉 bench 的组合必须因依赖（同时也因承诺）被否。
        missing = next(
            item for item in self.result.infeasible if item.projects == ("mark",)
        )
        self.assertTrue(any("成套依赖" in reason for reason in missing.reasons))

    def test_retired_projects_are_never_offered(self):
        for portfolio in self.result.portfolios:
            self.assertTrue(
                set(portfolio.projects).isdisjoint({"ramp", "light", "seat"})
            )


class PortfolioComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.mapping, self.ledger = _setup()
        self.feasibility = enumerate_feasible_portfolios(
            self.tables["projects"], self.tables["commitments"], 110, 1
        )
        self.relations = all_portfolio_relations(
            self.feasibility, self.ledger, self.mapping
        )
        self.by_pair = {(r.left, r.right): r for r in self.relations}
        self.by_code = {p.code: p for p in self.feasibility.portfolios}

    def test_superset_is_structural_not_a_preference_win(self):
        relation = self.by_pair[("P1", "P3")]
        self.assertTrue(relation.structural)
        self.assertEqual(relation.status, "structural")
        self.assertEqual(relation.left_unique, ())
        self.assertEqual(relation.right_unique, ("path",))

    def test_unique_items_best_preference_uses_each_resident_once(self):
        # P1(bench,mark) vs P2(bench,path)：独有项 mark 对 path。
        relation = self.by_pair[("P1", "P2")]
        self.assertEqual(relation.left_unique, ("mark",))
        self.assertEqual(relation.right_unique, ("path",))
        # mark>path：r2/r4/r6 共 8；r1 口径不同 + r3 明确未知共 5。
        self.assertEqual(relation.left_weight, 8)
        self.assertEqual(relation.right_weight, 0)
        self.assertEqual(relation.unknown_weight, 5)
        self.assertEqual(relation.status, "robust_win")

    def test_contestable_portfolio_pair_is_retained(self):
        relation = self.by_pair[("P4", "P5")]
        self.assertFalse(relation.structural)
        # P4 独有 play，P5 独有 tree；tree>play 共 5，未知 8 可翻转。
        self.assertEqual(relation.left_weight, 0)
        self.assertEqual(relation.right_weight, 5)
        self.assertEqual(relation.status, "contestable")

    def test_dominated_set_and_contenders(self):
        dominated = dominated_portfolios(self.feasibility, self.relations)
        # P2 被 P1/P4/P5 稳健占优；P3 被 P5 稳健占优。
        self.assertEqual(dominated["P2"], ["P1", "P4", "P5"])
        self.assertEqual(dominated["P3"], ["P5"])
        contenders = {
            p.code for p in self.feasibility.portfolios if p.code not in dominated
        }
        self.assertEqual(contenders, {"P1", "P4", "P5"})

    def test_best_relation_ignores_ties_for_vote_counting(self):
        r2 = next(o for o in self.ledger.opinions if o.resident == "r2")
        # P4 独有 play，P5 独有 tree：r2 的 tree>play 明确，应投给右方。
        relation, _ = compare_portfolios.__globals__["_best_relation"](
            r2, ("play",), ("tree",), self.mapping
        )
        self.assertEqual(relation, "right")
        # 同档最佳项并列时不加票。
        r4 = next(o for o in self.ledger.opinions if o.resident == "r4")
        relation, _ = _best_relation(
            r4, ("mark",), ("tree",), self.mapping
        )
        self.assertEqual(relation, "tie")


class WorkbenchTests(unittest.TestCase):
    def test_report_keeps_evidence_and_reasons(self):
        workspace = run_workspace(DATA)
        report = build_report(workspace)
        self.assertEqual(report["保留的竞争组合"], ["P1", "P4", "P5"])
        self.assertTrue(report["待确认问题"])
        # 暂缓组合必须带理由，便于会上对照。
        for item in report["暂缓组合"]:
            self.assertTrue(item["暂缓理由"])
        # 结论保留多个竞争方案，不伪造共识。
        names = {item["编号"] for item in report["结论"]["竞争方案"]}
        self.assertEqual(names, {"P1", "P4", "P5"})

    def test_public_report_has_no_person_ballot_link(self):
        report = public_report(build_report(run_workspace(DATA)))
        import re

        text = re.sub(r"\s", "", __import__("json").dumps(report, ensure_ascii=False))
        for identifier in ("r1", "r2", "r3", "r4", "r5", "r6", "b1", "b8", "b9"):
            self.assertNotIn(identifier, text)
        ledger = report["有效意见台账"]
        self.assertEqual(
            ledger["分区有效意见"],
            [
                {"片区": "north", "有效意见份数": 3, "计入权重": 9},
                {"片区": "south", "有效意见份数": 2, "计入权重": 4},
            ],
        )

    def test_review_after_new_confirmation_can_change_result(self):
        # 新增一份 r4 的确认意见（在截止前）改变其 play/tree 关系后应重审。
        tables = load_tables(DATA)
        mapping = build_project_mapping(tables["projects"])
        revised = dict(tables["ballots"][3])  # b4: r4 原意见
        revised["receipt"] = "b10"
        revised["received"] = "2026-04-12T10:00:00+08:00"
        revised["ranked"] = "tree;mark>path>play"
        ballots = tables["ballots"] + [revised]
        ledger = build_opinion_ledger(
            tables["residents"], ballots, mapping, WEIGHTS, START, DEADLINE
        )
        r4 = next(o for o in ledger.opinions if o.resident == "r4")
        self.assertEqual(r4.source_receipts, ("b10",))
        pairs = {(p.left, p.right): p for p in build_pair_preferences(ledger, mapping)}
        # r4 仍为 tree>play，关系方向不变但该机制保证整票替换可重算。
        self.assertEqual(pairs[("play", "tree")].right_weight, 5)

    def test_resident_without_ballot_keeps_weight_seat(self):
        # 资格居民未交任何票：权重仍计入总额，但所有比较都落入未知。
        tables = load_tables(DATA)
        mapping = build_project_mapping(tables["projects"])
        ballots = [row for row in tables["ballots"] if row["resident"] != "r6"]
        ledger = build_opinion_ledger(
            tables["residents"], ballots, mapping, WEIGHTS, START, DEADLINE
        )
        r6 = next(o for o in ledger.opinions if o.resident == "r6")
        self.assertEqual(r6.weight, 3)
        self.assertEqual(r6.source_receipts, ())
        self.assertEqual(ledger.total_weight, 13)
        for pair in build_pair_preferences(ledger, mapping):
            self.assertEqual(
                pair.left_weight + pair.right_weight + pair.tie_weight + pair.unknown_weight,
                13,
            )

    def test_same_timestamp_confirmation_takes_later_record(self):
        # 同一时刻的两份确认件按记录顺序后者生效，且仍只有一份有效意见。
        tables = load_tables(DATA)
        mapping = build_project_mapping(tables["projects"])
        duplicate = dict(tables["ballots"][7])  # b8: r2
        duplicate["receipt"] = "b90"
        duplicate["ranked"] = "play>bench"
        ledger = build_opinion_ledger(
            tables["residents"],
            tables["ballots"] + [duplicate],
            mapping,
            WEIGHTS,
            START,
            DEADLINE,
        )
        r2_opinions = [o for o in ledger.opinions if o.resident == "r2"]
        self.assertEqual(len(r2_opinions), 1)
        self.assertEqual(r2_opinions[0].source_receipts, ("b90",))


if __name__ == "__main__":
    unittest.main()
