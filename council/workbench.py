"""工作台汇总：把证据、可行方案、暂缓理由组织成主持人可用的报告。

报告分两层：

- ``build_report`` 为议事会内部工作稿，保留票册编号以便核对当时的有效意见；
- ``public_report`` 为会上公开版，移除任何票与个人的对应关系，只保留按片区
  汇总的权重、匿名化的剔除原因和聚合后的映射说明。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .core import (
    FeasibilityResult,
    OpinionLedger,
    PairPreference,
    PortfolioRelation,
    ProjectMapping,
    active_commitments,
    all_portfolio_relations,
    build_opinion_ledger,
    build_pair_preferences,
    build_project_mapping,
    dominated_portfolios,
    enumerate_feasible_portfolios,
    parse_dt,
)
from inspect_inputs import load_tables

_STATUS_TEXT = {
    "robust_win": "稳健占优：可接受的未知补足全部计入反方后仍严格领先",
    "robust_loss": "稳健落后：可接受的未知补足全部计入正方后仍严格落后",
    "contestable": "争议：存在不违反原票的未知补足可以改变排序",
    "incomparable": "口径不同：没有任何同口径明确证据，不能直接比较",
    "structural": "结构问题：组合间存在包含关系，偏好票无法在同一口径下评价增项",
}


def _labels(projects: list[dict]) -> dict[str, str]:
    return {row["project"]: row["label"] for row in projects}


def _pair_questions(
    pairs: list[PairPreference],
) -> dict[tuple[str, str], PairPreference]:
    return {(p.left, p.right): p for p in pairs}


def _build_questions(
    pairs: list[PairPreference],
    relations: list[PortfolioRelation],
    contenders: set[str],
    labels: dict[str, str],
) -> list[dict]:
    """整理最能帮助确认排序的提问范围。

    两类：
    1. 竞争组合之间真实比较所跨越、且项目层仍属争议或口径不同的提案对；
    2. 竞争组合之间的结构包含关系，需要对增项与既有项补确认。
    """
    pair_index = _pair_questions(pairs)
    questions: list[dict] = []

    for relation in relations:
        if relation.left not in contenders or relation.right not in contenders:
            continue
        if relation.structural:
            if relation.left_unique:
                added, base = relation.left_unique, relation.right_unique
            else:
                added, base = relation.right_unique, relation.left_unique
            touched = []
            for new_item in added:
                if not base:
                    touched.append(
                        f"增项 {labels.get(new_item, new_item)} 相对已承诺基底是否值得动用预算"
                    )
                    continue
                for old_item in base:
                    key = tuple(sorted((new_item, old_item)))
                    pair = pair_index.get(key)
                    if pair is not None and pair.status in {
                        "contestable",
                        "incomparable",
                    }:
                        touched.append(
                            f"{labels.get(key[0], key[0])} 与 "
                            f"{labels.get(key[1], key[1])} 的相对顺位"
                        )
                    else:
                        touched.append(
                            f"增项 {labels.get(new_item, new_item)} 相对 "
                            f"{labels.get(old_item, old_item)} 的取舍与预算影响"
                        )
            questions.append(
                {
                    "类型": "结构包含待确认",
                    "组合比较": f"{relation.left} 对 {relation.right}",
                    "说明": relation.reason,
                    "建议提问范围": sorted(set(touched)),
                }
            )
            continue

        crossed = []
        for a in relation.left_unique:
            for b in relation.right_unique:
                key = tuple(sorted((a, b)))
                pair = pair_index.get(key)
                if pair is None:
                    continue
                if pair.status in {"contestable", "incomparable"}:
                    crossed.append(
                        {
                            "项目对": list(key),
                            "项目名称": [labels.get(key[0], key[0]), labels.get(key[1], key[1])],
                            "未知权重": pair.unknown_weight,
                            "项目层状态": _STATUS_TEXT[pair.status],
                        }
                    )
        if crossed:
            questions.append(
                {
                    "类型": "争议组合比较",
                    "组合比较": f"{relation.left} 对 {relation.right}",
                    "可改变结果的提案对": sorted(
                        crossed, key=lambda item: -item["未知权重"]
                    ),
                    "建议提问范围": "就上述提案对向未知居民补确认，未知补足不得违反原票",
                }
            )

    # 项目层仍有争议、但不直接决定当前竞争组合比较的提案对，单独列出。
    used_pairs = {
        tuple(item["项目对"])
        for question in questions
        for item in question.get("可改变结果的提案对", [])
    }
    standalone = []
    for pair in pairs:
        if pair.status not in {"contestable", "incomparable"}:
            continue
        key = (pair.left, pair.right)
        if key in used_pairs:
            continue
        standalone.append(
            {
                "项目对": list(key),
                "项目名称": [labels[pair.left], labels[pair.right]],
                "未知权重": pair.unknown_weight,
                "项目层状态": _STATUS_TEXT[pair.status],
                "未知原因": pair.unknown_reasons,
            }
        )
    if standalone:
        questions.append(
            {
                "类型": "项目层未决争议",
                "建议提问范围": "这些提案对的证据尚不能区分顺位，应保留竞争排序并补确认",
                "提案对": sorted(standalone, key=lambda item: -item["未知权重"]),
            }
        )
    return questions


def run_workspace(folder: Path) -> dict:
    """读取资料目录并完成全部业务计算，返回内部工作稿。"""
    folder = Path(folder)
    tables = load_tables(folder)
    policy = json.loads((folder / "policy.json").read_text(encoding="utf-8"))
    start = parse_dt(policy["开议时间"])
    deadline = parse_dt(policy["意见截止"])

    mapping = build_project_mapping(tables["projects"])
    ledger = build_opinion_ledger(
        tables["residents"],
        tables["ballots"],
        mapping,
        policy["片区权重"],
        start,
        deadline,
    )
    pairs = build_pair_preferences(ledger, mapping)
    feasibility = enumerate_feasible_portfolios(
        tables["projects"],
        tables["commitments"],
        int(policy["总预算千元"]),
        int(policy["无障碍最低覆盖"]),
    )
    relations = all_portfolio_relations(feasibility, ledger, mapping)
    dominated = dominated_portfolios(feasibility, relations)
    labels = _labels(tables["projects"])
    contenders = {
        portfolio.code
        for portfolio in feasibility.portfolios
        if portfolio.code not in dominated
    }
    questions = _build_questions(list(pairs), list(relations), contenders, labels)

    return {
        "policy": policy,
        "tables": tables,
        "mapping": mapping,
        "ledger": ledger,
        "pairs": pairs,
        "feasibility": feasibility,
        "relations": relations,
        "dominated": dominated,
        "contenders": contenders,
        "questions": questions,
        "labels": labels,
    }


def _pair_public(pair: PairPreference, labels: dict[str, str]) -> dict:
    return {
        "左提案": pair.left,
        "左名称": labels[pair.left],
        "右提案": pair.right,
        "右名称": labels[pair.right],
        "支持左权重": pair.left_weight,
        "支持右权重": pair.right_weight,
        "并列权重": pair.tie_weight,
        "未知权重": pair.unknown_weight,
        "权重合计": pair.total_weight,
        "未知原因": pair.unknown_reasons,
        "状态": pair.status,
        "状态说明": _STATUS_TEXT[pair.status],
    }


def _relation_public(relation: PortfolioRelation, labels: dict[str, str]) -> dict:
    return {
        "左组合": relation.left,
        "右组合": relation.right,
        "左独有项": [f"{item}（{labels[item]}）" for item in relation.left_unique],
        "右独有项": [f"{item}（{labels[item]}）" for item in relation.right_unique],
        "支持左权重": relation.left_weight,
        "支持右权重": relation.right_weight,
        "并列权重": relation.tie_weight,
        "未知权重": relation.unknown_weight,
        "是否结构问题": relation.structural,
        "状态": relation.status,
        "状态说明": _STATUS_TEXT[relation.status],
        "依据": relation.reason,
    }


def build_report(workspace: dict) -> dict:
    """内部工作稿：含票册编号与居民对应，供议事会存档核对。"""
    mapping: ProjectMapping = workspace["mapping"]
    ledger: OpinionLedger = workspace["ledger"]
    feasibility: FeasibilityResult = workspace["feasibility"]
    labels = workspace["labels"]

    opinions = [
        {
            "居民": opinion.resident,
            "片区": opinion.district,
            "权重": opinion.weight,
            "来源票册": list(opinion.source_receipts),
            "票册版本": opinion.booklet or "无",
            "当期顺位": [list(tier) for tier in opinion.ranking],
            "明确未知": sorted(opinion.unknown),
            "旧口径留痕": [list(edge) for edge in opinion.legacy_edges],
            "处理说明": list(opinion.notes),
        }
        for opinion in ledger.opinions
    ]
    portfolios = [
        {
            "编号": p.code,
            "项目": list(p.projects),
            "项目名称": [labels[item] for item in p.projects],
            "费用千元": p.cost,
            "无障碍项数": p.access_count,
        }
        for p in feasibility.portfolios
    ]
    infeasible = [
        {
            "项目": list(item.projects),
            "项目名称": [labels.get(x, x) for x in item.projects],
            "暂缓理由": list(item.reasons),
        }
        for item in feasibility.infeasible
    ]
    return {
        "配置": workspace["policy"],
        "有效意见台账": {
            "资格居民权重合计": ledger.eligible_weight,
            "计入权重合计": ledger.total_weight,
            "权重核对": "权重按片区预定值全额计入，未因票册不完整而重分",
            "有效意见": opinions,
            "剔除票册": [
                {"票册": item.receipt, "居民": item.resident, "原因": item.reason}
                for item in ledger.excluded
            ],
        },
        "项目联审映射": {
            "当期提案": list(mapping.current),
            "退场提案": list(mapping.retired),
            "改名承接": [
                {"旧提案": old, "当期提案": new}
                for old, new in sorted(mapping.rename_of.items())
            ],
            "实质合并": [
                {"合并方案": new, "旧成员": list(old)}
                for new, old in sorted(mapping.merge_members.items())
            ],
        },
        "成对偏好证据": [_pair_public(pair, labels) for pair in workspace["pairs"]],
        "承诺与预算": {
            "生效承诺": list(feasibility.committed),
            "承诺占用预算千元": feasibility.committed_cost,
            "可安排预算千元": feasibility.remaining_budget,
        },
        "可行组合": portfolios,
        "暂缓组合": infeasible,
        "组合比较": [
            _relation_public(relation, labels) for relation in workspace["relations"]
        ],
        "被稳健占优的组合": workspace["dominated"],
        "保留的竞争组合": sorted(workspace["contenders"]),
        "待确认问题": workspace["questions"],
        "结论": _conclusions(workspace),
    }


def _conclusions(workspace: dict) -> dict:
    feasibility: FeasibilityResult = workspace["feasibility"]
    labels = workspace["labels"]
    contenders = [
        portfolio
        for portfolio in feasibility.portfolios
        if portfolio.code in workspace["contenders"]
    ]
    return {
        "竞争方案": [
            {
                "编号": p.code,
                "项目名称": [labels[item] for item in p.projects],
                "费用千元": p.cost,
            }
            for p in contenders
        ],
        "口径说明": [
            "现有偏好证据无法区分的竞争方案全部保留，不任意指定单一排序为共识。",
            "被稳健占优的组合在任何不违反原票的未知补足下都无法翻盘，可先行搁置。",
            "结构包含关系不能靠偏好票解决，需对照预算与新增确认意见另行审议。",
            "新增确认意见到达后应重审全部计算；已签约承诺未经撤回继续占用预算。",
        ],
    }


def public_report(report: dict) -> dict:
    """公开版：移除票与个人的对应关系，仅保留片区级聚合与匿名剔除原因。"""
    ledger_section = report["有效意见台账"]
    district_counter: Counter[str] = Counter()
    weight_counter: Counter[str] = Counter()
    note_counter: Counter[str] = Counter()
    empty_seats: list[dict] = []
    for opinion in ledger_section["有效意见"]:
        district_counter[opinion["片区"]] += 1
        weight_counter[opinion["片区"]] += opinion["权重"]
        if not opinion["来源票册"]:
            empty_seats.append({"片区": opinion["片区"], "保留权重": opinion["权重"]})
        for note in opinion["处理说明"]:
            note_counter[note] += 1

    anonymous_excluded = [
        {"匿名编号": f"X{index}", "原因": item["原因"]}
        for index, item in enumerate(
            sorted(ledger_section["剔除票册"], key=lambda item: item["票册"]), 1
        )
    ]
    public = json.loads(json.dumps(report, ensure_ascii=False))
    public["有效意见台账"] = {
        "资格居民权重合计": ledger_section["资格居民权重合计"],
        "计入权重合计": ledger_section["计入权重合计"],
        "权重核对": ledger_section["权重核对"],
        "分区有效意见": [
            {
                "片区": district,
                "有效意见份数": district_counter[district],
                "计入权重": weight_counter[district],
            }
            for district in sorted(district_counter)
        ],
        "未回收意见的权重席位": empty_seats,
        "剔除票册（匿名）": anonymous_excluded,
        "映射处理汇总": [
            {"说明": note, "出现次数": count}
            for note, count in sorted(note_counter.items())
        ],
    }
    return public
