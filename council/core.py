"""核心业务逻辑：有效意见、映射、成对偏好、可行组合与组合比较。

规则要点（与交接说明对应）：

1. 每名在册且 eligible 的居民至多有一份有效意见；确认件按整票替换此前意见，
   不拼接两份排序；transcribed（代录未确认）不覆盖原意见；逾期或来自不具
   资格居民的票册不进入台账。
2. 片区权重预先议定，按居民计入；一份旧票只回答了部分内容不会导致权重被
   重新分配给其他人。
3. 改名（rename）承接原顺位；实质合并（merge）不继承整包支持，旧表只能
   约束它明确比较过的内容；旧册把合并成员与合并方案放在不同口径下，旧票
   对合并方案的比较记为「口径不同」而不是支持或反对。
4. 没有明确反对不等于支持：票面上未出现的两两比较一律记为未知。
5. 预算办只考虑满足费用上限、成套依赖、工程互斥和无障碍保障的组合；已签约
   承诺（未撤回）继续占预算并强制进入每个候选组合。
6. 组合比较口径（见 policy.json）：比较居民对两个组合各自独有组成项的最佳
   明确偏好；多个最佳并列不加票；未知补足不能违反原票；每个居民在一次组合
   比较中只用一次权重。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Opinion:
    """一份有效意见（已折到当期提案空间）。

    ``ranking`` 为当期提案编号的档位列表，靠前的档位顺位更高；同档内并列。
    ``unknown`` 为居民明确表示未比较的当期提案编号集合。
    ``legacy_edges`` 保留旧册中明确写出、但无法折入当期口径的两两比较，
    形如 ``(左编号, 右编号, 关系)``，关系取 ``"left"``（左优于右）或
    ``"tie"``（并列），编号可能是已退场的旧提案。
    """

    resident: str
    district: str
    weight: int
    booklet: str
    received: datetime
    source_receipts: tuple[str, ...]
    ranking: tuple[tuple[str, ...], ...]
    unknown: frozenset[str]
    legacy_edges: tuple[tuple[str, str, str], ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExcludedBallot:
    """未进入台账的票册及其原因（内部留存，公开报告不含个人标识）。"""

    receipt: str
    resident: str
    reason: str


@dataclass(frozen=True)
class OpinionLedger:
    opinions: tuple[Opinion, ...]
    excluded: tuple[ExcludedBallot, ...]
    total_weight: int
    eligible_weight: int


@dataclass(frozen=True)
class ProjectMapping:
    """联审表整理出的当期/旧提案关系。"""

    current: tuple[str, ...]
    retired: tuple[str, ...]
    rename_of: dict[str, str]
    merge_members: dict[str, tuple[str, ...]]
    replaces: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class PairPreference:
    """一对当期提案 (left, right) 的汇总偏好证据。"""

    left: str
    right: str
    left_weight: int
    right_weight: int
    tie_weight: int
    unknown_weight: int
    unknown_reasons: dict[str, int]
    total_weight: int

    @property
    def status(self) -> str:
        """最坏未知补足下的关系。

        - ``robust_win``：即使全部未知票都补成反对，左方仍严格占优；
        - ``contestable``：存在不违反原票的未知补足可以改变排序；
        - ``incomparable``：双方都没有任何明确证据（口径全部不同），
          不能在相同口径下比较。
        """
        if self.left_weight == 0 and self.right_weight == 0 and self.tie_weight == 0:
            return "incomparable"
        if self.left_weight > self.right_weight + self.unknown_weight:
            return "robust_win"
        if self.right_weight > self.left_weight + self.unknown_weight:
            return "robust_loss"
        return "contestable"


@dataclass(frozen=True)
class Portfolio:
    code: str
    projects: tuple[str, ...]
    cost: int
    access_count: int


@dataclass(frozen=True)
class InfeasiblePortfolio:
    projects: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioRelation:
    left: str
    right: str
    left_unique: tuple[str, ...]
    right_unique: tuple[str, ...]
    left_weight: int
    right_weight: int
    tie_weight: int
    unknown_weight: int
    structural: bool
    reason: str

    @property
    def status(self) -> str:
        if self.structural:
            return "structural"
        if self.left_weight > self.right_weight + self.unknown_weight:
            return "robust_win"
        if self.right_weight > self.left_weight + self.unknown_weight:
            return "robust_loss"
        return "contestable"


@dataclass(frozen=True)
class FeasibilityResult:
    portfolios: tuple[Portfolio, ...]
    infeasible: tuple[InfeasiblePortfolio, ...]
    committed: tuple[str, ...]
    committed_cost: int
    remaining_budget: int


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def parse_dt(value: str) -> datetime:
    """按资料约定解析带时区的当地时间。"""
    return datetime.fromisoformat(value)


def parse_tiers(text: str) -> tuple[tuple[str, ...], ...]:
    """``a;b>c`` -> ``(("a", "b"), ("c",))``；空串为空排序。"""
    if not text:
        return ()
    tiers = []
    for tier in text.split(">"):
        items = tuple(part.strip() for part in tier.split(";") if part.strip())
        tiers.append(items)
    return tuple(tiers)


def parse_set(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(part.strip() for part in text.split(";") if part.strip())


def build_project_mapping(projects: list[dict]) -> ProjectMapping:
    current = tuple(p["project"] for p in projects if p["kind"] != "retired")
    retired = tuple(p["project"] for p in projects if p["kind"] == "retired")
    rename_of: dict[str, str] = {}
    merge_members: dict[str, tuple[str, ...]] = {}
    replaces: dict[str, tuple[str, ...]] = {}
    for row in projects:
        old = tuple(part.strip() for part in row["replaces"].split(";") if part.strip())
        if not old:
            continue
        replaces[row["project"]] = old
        if row["kind"] == "rename":
            for old_id in old:
                rename_of[old_id] = row["project"]
        elif row["kind"] == "merge":
            merge_members[row["project"]] = old
    return ProjectMapping(current, retired, rename_of, merge_members, replaces)


# ---------------------------------------------------------------------------
# 有效意见台账
# ---------------------------------------------------------------------------


def _fold_opinion(
    ballot: dict,
    resident_row: dict,
    weight: int,
    mapping: ProjectMapping,
) -> Opinion:
    """把一张确认票折到当期提案空间。"""
    raw_tiers = parse_tiers(ballot["ranked"])
    raw_unknown = parse_set(ballot["unknown"])
    folded_tiers: list[tuple[str, ...]] = []
    notes: list[str] = []
    legacy_edges: list[tuple[str, str, str]] = []

    is_legacy_booklet = ballot["booklet"] != "v2"

    for tier in raw_tiers:
        folded: list[str] = []
        for item in tier:
            if item in mapping.current:
                folded.append(item)
            elif item in mapping.rename_of:
                new_id = mapping.rename_of[item]
                folded.append(new_id)
                notes.append(f"改名承接：{item}→{new_id}，原顺位保留")
            elif any(item in members for members in mapping.merge_members.values()):
                notes.append(
                    f"实质合并不继承整包支持：{item} 已并入合并方案，"
                    "旧档位置不折入，相关比较列入旧口径留痕"
                )
            else:
                notes.append(f"已退场提案 {item} 无当期承接，顺位不折入")
        # 同档去重，保持首次出现顺序
        deduped = tuple(dict.fromkeys(folded))
        if deduped:
            folded_tiers.append(deduped)

    folded_unknown: set[str] = set()
    for item in raw_unknown:
        if item in mapping.current:
            folded_unknown.add(item)
        elif item in mapping.rename_of:
            folded_unknown.add(mapping.rename_of[item])
        elif any(item in members for members in mapping.merge_members.values()):
            notes.append(f"旧册明确未比较项 {item} 属合并成员，不折算为合并方案的未知")
        else:
            notes.append(f"旧册未知项 {item} 已退场，无当期承接")

    if is_legacy_booklet:
        # 留痕旧票明确写过的两两比较；这些比较只约束旧表本身，
        # 合并方案 path 不出现在旧册中，任何涉及 path 的当期比较都只能是未知。
        for upper, lower in combinations(range(len(raw_tiers)), 2):
            for hi in raw_tiers[upper]:
                for lo in raw_tiers[lower]:
                    legacy_edges.append((hi, lo, "left"))
        for tier in raw_tiers:
            for a, b in combinations(sorted(tier), 2):
                legacy_edges.append((a, b, "tie"))

    return Opinion(
        resident=resident_row["resident"],
        district=resident_row["district"],
        weight=weight,
        booklet=ballot["booklet"],
        received=parse_dt(ballot["received"]),
        source_receipts=(ballot["receipt"],),
        ranking=tuple(folded_tiers),
        unknown=frozenset(folded_unknown),
        legacy_edges=tuple(legacy_edges),
        notes=tuple(notes),
    )


def build_opinion_ledger(
    residents: list[dict],
    ballots: list[dict],
    mapping: ProjectMapping,
    weights: dict[str, int],
    start: datetime,
    deadline: datetime,
) -> OpinionLedger:
    """认定每名资格居民的唯一有效意见。

    同一居民的多张确认件以接收时间最晚者整票替换此前意见；接收时间相同时，
    以票册编号较大者为准，保证结果确定且可复算。
    """
    resident_by_id = {row["resident"]: row for row in residents}
    eligible_weight = sum(
        weights[row["district"]] for row in residents if row["eligible"] == "yes"
    )

    excluded: list[ExcludedBallot] = []
    chosen: dict[str, dict] = {}

    for ballot in ballots:
        resident = ballot["resident"]
        row = resident_by_id.get(resident)
        if row is None or row["eligible"] != "yes":
            excluded.append(ExcludedBallot(ballot["receipt"], resident, "居民不在合成资格名册或未确认资格"))
            continue
        received = parse_dt(ballot["received"])
        if received < start or received > deadline:
            excluded.append(ExcludedBallot(ballot["receipt"], resident, "意见落在议事会回收时间窗口之外"))
            continue
        if ballot["state"] != "confirmed":
            excluded.append(ExcludedBallot(ballot["receipt"], resident, "代录件未经本人确认，不覆盖原意见"))
            continue
        previous = chosen.get(resident)
        if previous is None or (
            received, ballot["receipt"]
        ) >= (parse_dt(previous["received"]), previous["receipt"]):
            chosen[resident] = ballot

    opinions: list[Opinion] = []
    used_weight = 0
    for row in residents:
        if row["eligible"] != "yes":
            continue
        resident = row["resident"]
        weight = weights[row["district"]]
        used_weight += weight
        ballot = chosen.get(resident)
        if ballot is None:
            # 资格居民未交有效意见：保留权重席位，但没有任何偏好证据。
            opinions.append(
                Opinion(
                    resident=resident,
                    district=row["district"],
                    weight=weight,
                    booklet="",
                    received=start,
                    source_receipts=(),
                    ranking=(),
                    unknown=frozenset(),
                    legacy_edges=(),
                    notes=("名册内居民未回收有效意见，权重保留但不计任何偏好",),
                )
            )
            continue
        opinions.append(_fold_opinion(ballot, row, weight, mapping))

    return OpinionLedger(
        opinions=tuple(opinions),
        excluded=tuple(excluded),
        total_weight=used_weight,
        eligible_weight=eligible_weight,
    )


# ---------------------------------------------------------------------------
# 成对偏好
# ---------------------------------------------------------------------------


def _tier_index(opinion: Opinion) -> dict[str, int]:
    return {
        project: index
        for index, tier in enumerate(opinion.ranking)
        for project in tier
    }


def pair_relation(
    opinion: Opinion, left: str, right: str, mapping: ProjectMapping
) -> tuple[str, str]:
    """返回单个居民对一对当期提案的关系与原因。

    关系取值：``left`` / ``right`` / ``tie`` / ``unknown``。
    """
    index = _tier_index(opinion)
    li = index.get(left)
    ri = index.get(right)
    if li is not None and ri is not None:
        if li < ri:
            return "left", "明确顺位"
        if li > ri:
            return "right", "明确顺位"
        return "tie", "同档并列"

    # 旧册口径：合并方案不在旧票中，旧成员的顺位不能折算成对它的支持。
    if opinion.booklet != "v2":
        merged = set(mapping.merge_members)
        if left in merged or right in merged:
            return "unknown", "旧册与合并方案口径不同，旧表未直接比较"
        if li is None and ri is None:
            return "unknown", "旧票未比较该对内容"
        return "unknown", "旧票只回答了部分内容，未明确该比较"

    if left in opinion.unknown or right in opinion.unknown:
        return "unknown", "票册明确标注未比较"
    if li is None and ri is None:
        return "unknown", "两项均未出现在该意见中"
    return "unknown", "仅一项出现在意见中，未明确该比较"


def build_pair_preferences(
    ledger: OpinionLedger, mapping: ProjectMapping
) -> tuple[PairPreference, ...]:
    result = []
    current = mapping.current
    for left, right in combinations(current, 2):
        left_w = right_w = tie_w = unknown_w = 0
        reasons: dict[str, int] = {}
        for opinion in ledger.opinions:
            relation, reason = pair_relation(opinion, left, right, mapping)
            if relation == "left":
                left_w += opinion.weight
            elif relation == "right":
                right_w += opinion.weight
            elif relation == "tie":
                tie_w += opinion.weight
            else:
                unknown_w += opinion.weight
                reasons[reason] = reasons.get(reason, 0) + opinion.weight
        result.append(
            PairPreference(
                left=left,
                right=right,
                left_weight=left_w,
                right_weight=right_w,
                tie_weight=tie_w,
                unknown_weight=unknown_w,
                unknown_reasons=dict(sorted(reasons.items())),
                total_weight=ledger.total_weight,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# 可行组合
# ---------------------------------------------------------------------------


def active_commitments(commitments: list[dict], projects: list[dict]) -> tuple[str, ...]:
    """手工承诺按登记生效；同一决议号的撤回使该承诺失效。"""
    project_ids = {row["project"] for row in projects}
    decisions: dict[str, str] = {}
    for row in commitments:
        if row["project"] not in project_ids:
            continue
        decisions[row["decision"]] = row["state"]
    active = []
    seen: set[str] = set()
    for row in commitments:
        if decisions.get(row["decision"]) != "signed":
            continue
        if row["state"] != "signed" or row["project"] in seen:
            continue
        seen.add(row["project"])
        active.append(row["project"])
    return tuple(active)


def _project_rows(projects: list[dict]) -> dict[str, dict]:
    return {row["project"]: row for row in projects}


def evaluate_portfolio(
    chosen: tuple[str, ...],
    project_rows: dict[str, dict],
    committed: tuple[str, ...],
    budget: int,
    min_access: int,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    chosen_set = set(chosen)

    missing_commitment = [c for c in committed if c not in chosen_set]
    if missing_commitment:
        reasons.append("未包含已签约且未撤回的承诺项目：" + "、".join(missing_commitment))

    cost = sum(int(project_rows[p]["cost"]) for p in chosen)
    if cost > budget:
        reasons.append(f"费用 {cost} 千元超过上限 {budget} 千元")

    for project in chosen:
        requires = parse_set(project_rows[project]["requires"])
        missing = sorted(requires - chosen_set)
        if missing:
            reasons.append(f"{project} 缺少成套依赖：" + "、".join(missing))
        excludes = parse_set(project_rows[project]["excludes"])
        clash = sorted(excludes & chosen_set)
        if clash:
            reasons.append(f"{project} 与不可同建项目冲突：" + "、".join(clash))

    access_count = sum(1 for p in chosen if project_rows[p]["access"] == "1")
    if access_count < min_access:
        reasons.append(f"无障碍覆盖 {access_count} 项，低于最低要求 {min_access} 项")

    return (not reasons), tuple(reasons)


def enumerate_feasible_portfolios(
    projects: list[dict],
    commitments: list[dict],
    budget: int,
    min_access: int,
) -> FeasibilityResult:
    project_rows = _project_rows(projects)
    selectable = [p for p in project_rows if project_rows[p]["kind"] != "retired"]
    committed = active_commitments(commitments, projects)
    committed_cost = sum(int(project_rows[c]["cost"]) for c in committed)

    feasible: list[Portfolio] = []
    infeasible: list[InfeasiblePortfolio] = []
    code = 0
    for size in range(len(selectable) + 1):
        for combo in combinations(sorted(selectable), size):
            ok, reasons = evaluate_portfolio(
                combo, project_rows, committed, budget, min_access
            )
            if ok:
                code += 1
                feasible.append(
                    Portfolio(
                        code=f"P{code}",
                        projects=combo,
                        cost=sum(int(project_rows[p]["cost"]) for p in combo),
                        access_count=sum(
                            1 for p in combo if project_rows[p]["access"] == "1"
                        ),
                    )
                )
            else:
                infeasible.append(InfeasiblePortfolio(combo, reasons))

    return FeasibilityResult(
        portfolios=tuple(feasible),
        infeasible=tuple(infeasible),
        committed=committed,
        committed_cost=committed_cost,
        remaining_budget=budget - committed_cost,
    )


# ---------------------------------------------------------------------------
# 组合比较
# ---------------------------------------------------------------------------


def _best_relation(
    opinion: Opinion,
    unique_left: tuple[str, ...],
    unique_right: tuple[str, ...],
    mapping: ProjectMapping,
) -> tuple[str, str]:
    """居民对两侧独有组成项的「最佳明确偏好」。

    只承认明确的优于关系；并列、未知都不加票。任一侧没有可明确比较的
    最佳项时按未知处理，且未知补足不得违反原票（由汇总端的最坏补足检验）。
    """
    index = _tier_index(opinion)
    left_ranked = [(index[p], p) for p in unique_left if p in index]
    right_ranked = [(index[p], p) for p in unique_right if p in index]

    if not left_ranked or not right_ranked:
        return "unknown", "一侧独有项未进入该居民的明确排序"

    best_left_tier = min(tier for tier, _ in left_ranked)
    best_right_tier = min(tier for tier, _ in right_ranked)
    if best_left_tier < best_right_tier:
        return "left", "对两侧最佳独有项的明确偏好"
    if best_right_tier < best_left_tier:
        return "right", "对两侧最佳独有项的明确偏好"
    return "tie", "两侧最佳独有项同档，并列不加票"


def compare_portfolios(
    left: Portfolio,
    right: Portfolio,
    ledger: OpinionLedger,
    mapping: ProjectMapping,
) -> PortfolioRelation:
    left_set = set(left.projects)
    right_set = set(right.projects)
    unique_left = tuple(sorted(left_set - right_set))
    unique_right = tuple(sorted(right_set - left_set))

    if not unique_left or not unique_right:
        # 包含关系：偏好票无法在同一口径下比较增项是否值得，属结构问题。
        if not unique_left and not unique_right:
            reason = "两个组合组成完全相同"
        elif not unique_left:
            reason = "左侧组合被右侧包含，偏好证据无法评价新增项：" + "、".join(unique_right)
        else:
            reason = "右侧组合被左侧包含，偏好证据无法评价新增项：" + "、".join(unique_left)
        return PortfolioRelation(
            left=left.code,
            right=right.code,
            left_unique=unique_left,
            right_unique=unique_right,
            left_weight=0,
            right_weight=0,
            tie_weight=0,
            unknown_weight=ledger.total_weight,
            structural=True,
            reason=reason,
        )

    left_w = right_w = tie_w = unknown_w = 0
    for opinion in ledger.opinions:
        relation, _ = _best_relation(opinion, unique_left, unique_right, mapping)
        if relation == "left":
            left_w += opinion.weight
        elif relation == "right":
            right_w += opinion.weight
        elif relation == "tie":
            tie_w += opinion.weight
        else:
            unknown_w += opinion.weight

    return PortfolioRelation(
        left=left.code,
        right=right.code,
        left_unique=unique_left,
        right_unique=unique_right,
        left_weight=left_w,
        right_weight=right_w,
        tie_weight=tie_w,
        unknown_weight=unknown_w,
        structural=False,
        reason="按各自独有组成项的最佳明确偏好比较；每个居民只计一次权重",
    )


def all_portfolio_relations(
    feasibility: FeasibilityResult,
    ledger: OpinionLedger,
    mapping: ProjectMapping,
) -> tuple[PortfolioRelation, ...]:
    relations = []
    for left, right in combinations(feasibility.portfolios, 2):
        relations.append(compare_portfolios(left, right, ledger, mapping))
    return tuple(relations)


def dominated_portfolios(
    feasibility: FeasibilityResult,
    relations: tuple[PortfolioRelation, ...],
) -> dict[str, list[str]]:
    """返回被稳健占优的组合代码 -> 占优它的组合代码列表。"""
    dominated: dict[str, list[str]] = {p.code: [] for p in feasibility.portfolios}
    for relation in relations:
        if relation.status == "robust_win":
            dominated[relation.right].append(relation.left)
        elif relation.status == "robust_loss":
            dominated[relation.left].append(relation.right)
    return {code: sorted(by) for code, by in dominated.items() if by}
