from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

from filmqc import (
    Repository,
    assess_rolls,
    build_relations,
    build_routes,
    certificate_view,
    explain_hold,
    retest_board,
)
from filmqc.decisions import DISPUTE, HOLD, PASS, RETEST, partial_subset

STATUS_CN = {
    PASS: '可直接沿用',
    RETEST: '等待可接受复测',
    DISPUTE: '路线争议，扣住待裁决',
    HOLD: '扣住',
}


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(f'无法序列化 {type(value)!r}')


def render_text(repo: Repository, as_of: date) -> str:
    relations = build_relations(repo, as_of)
    routes = build_routes(repo, as_of, relations)
    rolls = assess_rolls(repo, as_of)
    certs = certificate_view(repo, as_of, rolls)
    board = retest_board(repo, as_of)
    lines = [f'判断时点：{as_of.isoformat()}（以读数 received / 复测 slot 为可见时点）', '']

    lines.append('一、换算关系（适用范围限本系列与拟合测量区间，禁止外推）')
    for (src, tgt), rel in sorted(relations.items()):
        if not rel.supported:
            lines.append(f'  {src}->{tgt}：不支持 —— {rel.note}')
            continue
        lines.append(
            f'  {src}->{tgt}：y = {rel.intercept:.4f} + {rel.slope:.6f}·x，'
            f'拟合片 {sorted(rel.fit_sheets)}，区间 {rel.fit_range[0]:.1f}~'
            f'{rel.fit_range[1]:.1f}，独立留出片 {sorted(rel.holdout_sheets)}，'
            f'留出最大误差 {rel.holdout_error:.3f}')
    lines.append('')

    lines.append('二、可比较路线（两次换算的共用标准片只计一份独立证据）')
    for origin, options in sorted(routes.items()):
        for route in options:
            tag = '直连' if route.direct else '两次换算'
            lines.append(
                f'  {origin}: {"->".join(route.legs)}（{tag}），'
                f'独立留出片 {sorted(route.independent_holdouts)}，'
                f'总误差上界 {route.holdout_error:.3f}')
    lines.append('')

    lines.append('三、卷材逐订单判定')
    for roll in rolls:
        lines.append(f'  卷材 {roll.lot}（检件 {", ".join(roll.slices)}，'
                     f'产线读头 {roll.line_instrument}，原读值 {roll.line_value}）')
        for oid, verdict in roll.order_verdicts.items():
            order = repo.orders[oid]
            lines.append(f'    {oid} [{STATUS_CN[verdict.status]}] '
                         f'规格 {order.min:g}~{order.max:g}，允许总误差 '
                         f'{order.allowed_error:g}，line_only={int(order.line_only)}，'
                         f'lab_allowed={int(order.lab_allowed)}')
            if verdict.direct_r0 is not None:
                lines.append(f'      R0 直读/复测：{verdict.direct_r0:.3f}'
                             f'（订单是否接受见许可标记）')
            for evaluation in verdict.routes:
                legs = '->'.join(evaluation.route.legs)
                mark = '可采纳' if evaluation.admissible else '不可采纳'
                lines.append(
                    f'      路线 {legs}：预测 R0={evaluation.predicted:.3f}，'
                    f'独立证据误差 {evaluation.route.holdout_error:.3f}（{mark}）')
            for reason in verdict.reasons:
                lines.append(f'      · {reason}')
            for note in verdict.remediation:
                lines.append(f'      补救：{note}')
    lines.append('')

    lines.append('四、证书跨卷验收')
    for view in certs:
        frozen = '已交付（冻结，仅附影响说明）' if view.frozen else '未签发'
        group_mean = (f'{view.group_values[0]:.3f}'
                      if view.group_values else '无可用证据')
        lines.append(f'  证书 {view.certificate_id}（{frozen}）：{view.issue_status}，'
                     f'组级判定 {"通过" if view.group_pass else "不通过"}，'
                     f'整组共享偏移误差均值 {group_mean} '
                     f'（上界 {repo.certificates[view.certificate_id].group_max_mean_error:g}）')
        for line in explain_hold(view):
            lines.append(f'      · {line}')
        if view.issue_status == 'partial':
            subset = partial_subset(repo, view)
            cert = repo.certificates[view.certificate_id]
            if subset:
                lines.append(f'      合同允许部分交付：可先就 {list(subset)} '
                             '开拆分证书，争议/待补卷材不得混入客户要求的整组核验')
            else:
                lines.append('      即使部分交付也不存在满足组界的子集，全部挂起')
        if view.impact_note:
            lines.append(f'      影响说明：{view.impact_note}')
            for change in view.impact_changes:
                lines.append(f'        - {change["type"]}：{change}')
    lines.append('')

    lines.append('五、复测工作台')
    for item in board:
        kind_cn = '卷材取样' if item.kind == 'roll_sampling' else '标准片补证'
        if item.kind == 'roll_sampling':
            coverage = f'可覆盖 {list(item.covers_orders) or "—"}'
        else:
            coverage = f'可恢复证书共同路径 {list(item.restores_certificates)}'
        lines.append(f'  {item.retest_id} {item.slice_id}/{item.instrument} '
                     f'{item.state} {item.slot.isoformat()}（{kind_cn}），{coverage}')
        lines.append(f'      {item.note}')
        if item.duplicates:
            lines.append(f'      重复占位申请会被拒绝，已有未完成复测：{list(item.duplicates)}')
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='薄膜分光跨尺度可比较性与出货判定')
    parser.add_argument('directory', type=Path, nargs='?', default=Path('data'))
    parser.add_argument('--as-of', type=date.fromisoformat, default=date(2026, 4, 10))
    parser.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    args = parser.parse_args()
    repo = Repository(args.directory)
    if args.json:
        payload = {
            'as_of': args.as_of,
            'rolls': assess_rolls(repo, args.as_of),
            'certificates': certificate_view(repo, args.as_of),
            'retests': retest_board(repo, args.as_of),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2,
                         default=_json_default))
    else:
        print(render_text(repo, args.as_of))


if __name__ == '__main__':
    main()
