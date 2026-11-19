"""命令行入口：输出工作台报告。

用法：

    python3 -m council.report                 # 内部工作稿（含票册编号）
    python3 -m council.report --public        # 会上公开版（无票-个人对应）
    python3 -m council.report --text          # 紧凑文本摘要
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .workbench import build_report, public_report, run_workspace


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append("== 社区微更新稳健组合决议 ==")
    ledger = report["有效意见台账"]
    lines.append(
        f"计入权重 {ledger['计入权重合计']} / 资格权重 {ledger['资格居民权重合计']}；"
        f"剔除票册 {len(ledger.get('剔除票册', ledger.get('剔除票册（匿名）', [])))} 份"
    )
    lines.append("")
    lines.append("-- 成对偏好证据 --")
    for pair in report["成对偏好证据"]:
        lines.append(
            f"{pair['左提案']} vs {pair['右提案']}: "
            f"左 {pair['支持左权重']} / 右 {pair['支持右权重']} / "
            f"并列 {pair['并列权重']} / 未知 {pair['未知权重']} -> {pair['状态说明']}"
        )
    lines.append("")
    lines.append("-- 可行组合 --")
    for portfolio in report["可行组合"]:
        lines.append(
            f"{portfolio['编号']}: {'+'.join(portfolio['项目'])} "
            f"费用 {portfolio['费用千元']} 千元，无障碍 {portfolio['无障碍项数']} 项"
        )
    lines.append("")
    lines.append("-- 组合比较 --")
    for relation in report["组合比较"]:
        lines.append(
            f"{relation['左组合']} vs {relation['右组合']}: "
            f"左 {relation['支持左权重']} / 右 {relation['支持右权重']} / "
            f"并列 {relation['并列权重']} / 未知 {relation['未知权重']} "
            f"-> {relation['状态说明']}"
        )
    lines.append("")
    lines.append(f"被稳健占优：{report['被稳健占优的组合'] or '无'}")
    lines.append(f"保留竞争组合：{', '.join(report['保留的竞争组合'])}")
    lines.append("")
    lines.append("-- 待确认问题 --")
    for question in report["待确认问题"]:
        lines.append(f"[{question['类型']}] {json.dumps(question, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parents[1] / "data",
    )
    parser.add_argument("--public", action="store_true", help="输出脱敏公开版")
    parser.add_argument("--text", action="store_true", help="输出文本摘要而非 JSON")
    args = parser.parse_args(argv)

    workspace = run_workspace(args.folder)
    report = build_report(workspace)
    if args.public:
        report = public_report(report)
    if args.text:
        print(render_text(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
