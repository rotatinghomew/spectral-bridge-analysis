from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .conversion import (
    REFERENCE,
    RouteEvaluation,
    build_relations,
    build_routes,
    evaluate_route,
)
from .pairing import observed_means
from .repository import Order, Repository
from .retests import retest_board

PASS = 'pass'            # 可按当前证据直接沿用
RETEST = 'retest'        # 当前证据不足，但有可接受的复测可补齐
DISPUTE = 'dispute'      # 两条有数据支持的路线结论不一致，需人工裁决
HOLD = 'hold'            # 扣住：无可用路线且无补救复测

# 争议子类型：规格材料判定冲突 / 独立证据误差预算冲突
DISPUTE_MATERIAL = 'material'
DISPUTE_BUDGET = 'budget'


@dataclass
class OrderVerdict:
    order_id: str
    status: str
    routes: tuple[RouteEvaluation, ...]
    direct_r0: float | None
    direct_r0_passes: bool | None
    reasons: tuple[str, ...]
    remediation: tuple[str, ...]
    evidence_errors: tuple[float, ...]  # 各条可采纳证据的独立标准片误差；R0 直读为 0
    dispute_kind: str = ''              # material / budget，仅争议时非空


@dataclass
class RollAssessment:
    lot: str
    slices: tuple[str, ...]
    line_instrument: str | None
    line_value: float | None
    order_verdicts: dict[str, OrderVerdict]


def _slice_means(repo: Repository, as_of: date) -> dict[tuple[str, str], float]:
    return observed_means(repo, as_of)


def _r0_direct(slice_id: str, means) -> float | None:
    values = [v for (sid, inst), v in means.items()
              if sid == slice_id and inst == REFERENCE]
    return values[0] if values else None


def _margin_pass(order: Order, value: float) -> bool:
    return (order.min - order.allowed_error <= value <= order.max + order.allowed_error)


def _within_spec(order: Order, value: float) -> bool:
    return order.min <= value <= order.max


def _route_admissible_for_order(evaluation: RouteEvaluation, order: Order,
                                origin: str) -> tuple[bool, tuple[str, ...]]:
    # 产线读头换算后的结果仍属产线取样，line_only 订单同样可以采纳；
    # 质控室 R0 直读的采样许可在 assess_rolls 中按 lab_allowed 单独处理。
    return evaluation.admissible, evaluation.reasons


def assess_rolls(repo: Repository, as_of: date) -> list[RollAssessment]:
    """对每张订单卷材给出逐订单判定与补救路径。"""
    means = _slice_means(repo, as_of)
    relations = build_relations(repo, as_of)
    routes_by_origin = build_routes(repo, as_of, relations)
    board = {item.slice_id: item for item in retest_board(repo, as_of)}

    assessments: list[RollAssessment] = []
    lots = sorted({order.lot for order in repo.orders.values()})
    for lot in lots:
        slices = repo.order_slices(lot)
        slice_ids = tuple(s.id for s in slices)
        line_value = line_inst = None
        for slice_id in slice_ids:
            for (sid, inst), value in means.items():
                if sid == slice_id and repo.instruments[inst].site == 'LINE':
                    line_value, line_inst = value, inst
        verdicts: dict[str, OrderVerdict] = {}
        for order in (o for o in repo.orders.values() if o.lot == lot):
            direct_value = next((v for sid in slice_ids
                                 if (v := _r0_direct(sid, means)) is not None), None)
            direct_pass = (_margin_pass(order, direct_value)
                           if direct_value is not None else None)
            evaluations: list[RouteEvaluation] = []
            if line_inst is not None:
                for route in routes_by_origin.get(line_inst, ()):
                    evaluation = evaluate_route(route, line_value,
                                                order.allowed_error)
                    evaluations.append(evaluation)
            material_decisions: set[bool] = set()
            remediation: list[str] = []
            reasons: list[str] = []
            evidence_errors: list[float] = []
            # R0 原尺度直读：line_only 订单不接收质控室取样；
            # 其余订单按 lab_allowed 决定能否作为该采样许可下的证据。
            if direct_value is not None:
                if order.line_only:
                    reasons.append('订单 line_only=1：质控室 R0 复测不能作为放行依据')
                elif not order.lab_allowed:
                    reasons.append('订单未开放 lab_allowed：R0 复测证据不可用')
                else:
                    evidence_errors.append(0.0)
                    material_decisions.add(bool(direct_pass))
                    if not direct_pass:
                        reasons.append(
                            f'R0 直读 {direct_value:.3f} 超出含误差带验收边界')
            for evaluation in evaluations:
                # 注意：即使独立标准片误差超过订单允许总误差，该路线仍是
                # “有数据支持的路线”，其材料判定必须参与争议比较，
                # 不能悄悄剔除后默认走较有利的路线。
                budget_ok, _ = _route_admissible_for_order(
                    evaluation, order, line_inst)
                legs = '→'.join(evaluation.route.legs)
                if not evaluation.applicable:
                    reasons.extend(evaluation.reasons)
                    continue
                if budget_ok:
                    evidence_errors.append(evaluation.route.holdout_error)
                inside = _within_spec(order, evaluation.predicted)
                margin = _margin_pass(order, evaluation.predicted)
                material_decisions.add(margin)
                if not margin:
                    reasons.append(
                        f'路线 {legs} 预测 {evaluation.predicted:.3f} '
                        '超出含误差带验收边界')
                elif not inside:
                    reasons.append(
                        f'路线 {legs} 预测 {evaluation.predicted:.3f} '
                        '落在误差带内但不在规格区间')
                if not budget_ok:
                    reasons.append(
                        f'路线 {legs} 独立标准片总误差 '
                        f'{evaluation.route.holdout_error:.3f} 超出订单允许总误差 '
                        f'{order.allowed_error:.3f}，不能单独放行')
            pending_retests = [board[sid] for sid in slice_ids if sid in board]
            for item in pending_retests:
                if item.kind != 'roll_sampling':
                    continue  # 标准片补证不能冒充订单卷材取样
                if item.instrument == REFERENCE:
                    if order.line_only:
                        remediation.append(
                            f'复测 {item.retest_id} 为 R0 质控室取样，'
                            'line_only 订单不可用，仅服务同卷其他订单')
                    elif order.lab_allowed:
                        remediation.append(
                            f'复测 {item.retest_id}（R0 卷材取样）'
                            f'可在 {item.slot:%Y-%m-%d %H:%M} 后补齐证据')
                else:
                    remediation.append(
                        f'复测 {item.retest_id}（{item.instrument} 产线取样）'
                        f'可在 {item.slot:%Y-%m-%d %H:%M} 后补齐证据')
            supported = [e for e in evaluations if e.applicable]
            budget_rejected = [e for e in supported if not e.admissible]
            dispute_kind = ''
            if True in material_decisions and False in material_decisions:
                status = DISPUTE
                dispute_kind = DISPUTE_MATERIAL
                reasons.append('两条有数据支持的路线材料判定不一致，禁止默认取较有利路线')
                dominant = _dominant_error_source(supported)
                if dominant:
                    reasons.append(dominant)
            elif supported and budget_rejected and evidence_errors:
                # 同一卷材上一条路线满足订单总误差、另一条有数据支持却超预算：
                # 这是证据可比较性争议，必须显式呈现而非默认选有利路线。
                status = DISPUTE
                dispute_kind = DISPUTE_BUDGET
                reasons.append(
                    '两条有数据支持的路线在允许总误差上结论不一致，'
                    '按争议挂起并展示主要误差来源，禁止默认取较有利路线放行')
                dominant = _dominant_error_source(supported)
                if dominant:
                    reasons.append(dominant)
            elif material_decisions == {True} and evidence_errors:
                status = PASS
            elif any('可在' in note for note in remediation):
                status = RETEST
            else:
                status = HOLD
            verdicts[order.id] = OrderVerdict(
                order.id, status, tuple(evaluations), direct_value,
                direct_pass, tuple(dict.fromkeys(reasons)),
                tuple(dict.fromkeys(remediation)),
                tuple(evidence_errors), dispute_kind)
        assessments.append(RollAssessment(lot, slice_ids, line_inst,
                                          line_value, verdicts))
    return assessments


def _dominant_error_source(evaluations: list[RouteEvaluation]) -> str:
    chain = [e for e in evaluations if not e.route.direct]
    direct = [e for e in evaluations if e.route.direct]
    if not chain:
        return ''
    chain_error = max(e.route.holdout_error for e in chain)
    direct_error = max((e.route.holdout_error for e in direct), default=0.0)
    if chain_error > direct_error:
        return (f'主要误差来源：两次换算链的独立标准片总误差 {chain_error:.3f}，'
                f'高于直连路线 {direct_error:.3f}；两段共用标准片不重复计证据，'
                '独立留出片数量有限')
    return ''


@dataclass
class CertificateView:
    certificate_id: str
    frozen: bool
    per_order: dict[str, OrderVerdict]
    group_metric: str
    group_pass: bool
    group_values: tuple[float, ...]
    issue_status: str
    why_partial_fails: tuple[str, ...]
    impact_note: str
    dossier: dict
    impact_changes: tuple[dict, ...] = ()


def _impact_changes(repo: Repository, cert: Certificate,
                    issued: date, as_of: date) -> list[dict]:
    """比较签发时点与当前时点的证据差异，仅用于影响说明，不回写判断。"""
    from .pairing import build_pairs
    from .conversion import build_relations
    changes: list[dict] = []
    for pc in repo.pair_changes:
        if issued < pc.received <= as_of:
            changes.append({
                'type': 'swap_withdrawn',
                'received': pc.received.isoformat(),
                'slice': pc.slice,
                'swap': pc.withdraw_swap,
                'effect': '互换组撤销后相关检件不得再配对，原始读数保留',
            })
    old_pairs = build_pairs(repo, issued)
    new_pairs = build_pairs(repo, as_of)
    old_rels = build_relations(repo, issued, old_pairs)
    new_rels = build_relations(repo, as_of, new_pairs)
    for key, old in old_rels.items():
        new = new_rels.get(key)
        if new is None:
            continue
        if old.holdout_sheets != new.holdout_sheets:
            changes.append({
                'type': 'holdout_changed',
                'relation': f'{key[0]}->{key[1]}',
                'holdouts_at_issue': sorted(old.holdout_sheets),
                'holdouts_now': sorted(new.holdout_sheets),
                'effect': '独立留出标准片集合变化，关系误差需按新集合复核',
            })
    return changes


def certificate_view(repo: Repository, as_of: date,
                     assessments: list[RollAssessment] | None = None
                     ) -> list[CertificateView]:
    """按证书做跨卷组级验收，并给出开单说明与可追溯卷宗。"""
    if assessments is None:
        assessments = assess_rolls(repo, as_of)
    by_order = {vid: verdict for roll in assessments
                for vid, verdict in roll.order_verdicts.items()}
    views: list[CertificateView] = []
    for cert in repo.certificates.values():
        frozen = cert.issued is not None and cert.issued < as_of
        effective_as_of = cert.issued if frozen else as_of
        if frozen:
            # 已交付证书：按签发时点重建当时采用的读数、关系与判断并冻结，
            # 后续资料变化只进入影响说明，不回写采用的读数和判断。
            historical = assess_rolls(repo, effective_as_of)
            order_verdicts = {vid: verdict for roll in historical
                              for vid, verdict in roll.order_verdicts.items()
                              if vid in cert.orders}
            impact = (f'证书已于 {cert.issued} 交付：采用的读数与判断保持原样，'
                      '不回写、不改判；以下为签发后资料变化的影响说明。')
            impact_changes = tuple(_impact_changes(
                repo, cert, cert.issued, as_of))
        else:
            order_verdicts = {oid: by_order[oid] for oid in cert.orders}
            impact = ''
            impact_changes = ()
        group_values, why, group_pass = _group_check(
            repo, cert, order_verdicts, assessments, effective_as_of)
        statuses = {v.status for v in order_verdicts.values()}
        if frozen:
            issue_status = 'frozen'
        elif group_pass and statuses <= {PASS}:
            issue_status = 'issuable'
        elif cert.partial_allowed:
            issue_status = 'partial'
        else:
            issue_status = 'blocked'
        views.append(CertificateView(
            cert.id, frozen, order_verdicts,
            'common_reader_bias=1 时按同读头相关偏移取整组误差均值的上界，'
            '不允许逐卷误差独立平均抵消',
            group_pass, tuple(group_values), issue_status,
            tuple(dict.fromkeys(why)), impact,
            _dossier(repo, cert, order_verdicts, effective_as_of),
            impact_changes))
    return views


def _group_check(repo, cert, order_verdicts, assessments, as_of):
    values, why = [], []
    for oid in cert.orders:
        verdict = order_verdicts[oid]
        # R0 直读的独立证据误差记 0；evidence_errors 只含满足订单总误差、
        # 被实际采纳的路线。多条预算内路线并列时，组级按实际可采用的最小
        # 证据误差计；路线间争议由订单状态另行挂起，不在组级重复放大。
        # 对冻结证书，这里就是其签发当时采用的证据。
        route_errors = list(verdict.evidence_errors)
        if not route_errors:
            why.append(f'{oid} 没有可采纳的证据，无法并入整组核验')
            continue
        values.append(min(route_errors))
    if not values:
        return values, why, False
    # common_reader_bias=1：同读头偏移对整组同号，不假设逐卷独立抵消，
    # 直接以各卷证据误差均值作为组级共享偏移上界与合同界比较。
    group_mean = sum(values) / len(values)
    if group_mean > cert.group_max_mean_error:
        why.append(
            f'整组共享偏移误差均值 {group_mean:.3f} 超过证书 '
            f'group_max_mean_error {cert.group_max_mean_error:.3f}')
    return [group_mean], why, not why


def _dossier(repo, cert, order_verdicts, as_of) -> dict:
    dossier = {'as_of': as_of.isoformat(), 'orders': {}}
    for oid in cert.orders:
        order = repo.orders[oid]
        verdict = order_verdicts[oid]
        entry = {
            'lot': order.lot,
            'spec': [order.min, order.max],
            'allowed_error': order.allowed_error,
            'line_only': order.line_only,
            'lab_allowed': order.lab_allowed,
            'direct_r0': verdict.direct_r0,
            'routes': [
                {
                    'legs': list(e.route.legs),
                    'predicted_r0': round(e.predicted, 4),
                    'independent_holdouts': sorted(e.route.independent_holdouts),
                    'holdout_error': round(e.route.holdout_error, 4),
                    'admissible': e.admissible,
                    'reasons': list(e.reasons),
                    'relations': [
                        {
                            'pair': f'{r.source}->{r.target}',
                            'equation': f'R_target = {r.intercept:.4f} '
                                        f'+ {r.slope:.6f} * R_source',
                            'fit_sheets': sorted(r.fit_sheets),
                            'fit_range': list(r.fit_range),
                            'holdout_sheets': sorted(r.holdout_sheets),
                            'supported': r.supported,
                            'note': r.note,
                        }
                        for r in e.route.relations
                    ],
                }
                for e in verdict.routes
            ],
            'verdict': verdict.status,
            'reasons': list(verdict.reasons),
            'remediation': list(verdict.remediation),
        }
        dossier['orders'][oid] = entry
    return dossier


def partial_subset(repo: Repository, view: CertificateView) -> tuple[str, ...]:
    """合同允许部分交付时，返回仍能满足组级误差界的最大订单子集。

    客户要求整组核验的测量不得拆散；未开放部分交付时返回空元组。
    """
    cert = repo.certificates[view.certificate_id]
    if not cert.partial_allowed or view.frozen:
        return ()
    eligible = [oid for oid, verdict in view.per_order.items()
                if verdict.status == PASS and verdict.evidence_errors]
    # 组界对子集单调：从全部合格订单起，逐个剔除证据误差最大者直至满足界。
    eligible.sort(key=lambda oid: min(view.per_order[oid].evidence_errors),
                  reverse=True)
    while eligible:
        mean = sum(min(view.per_order[oid].evidence_errors)
                   for oid in eligible) / len(eligible)
        if mean <= cert.group_max_mean_error:
            return tuple(sorted(eligible))
        eligible.pop(0)
    return ()


def explain_hold(view: CertificateView) -> list[str]:
    """向开单人解释：为什么逐卷可用的证据无法组成一份证书。"""
    lines = []
    if view.group_pass and all(v.status == PASS for v in view.per_order.values()):
        lines.append('各卷材与整组均满足约定误差，证书可开。')
        return lines
    for oid, verdict in view.per_order.items():
        if verdict.status == DISPUTE:
            lines.append(f'{oid} 存在路线争议，争议卷材不能默认放行进入证书')
        elif verdict.status in (HOLD, RETEST):
            lines.append(f'{oid} 当前无可采纳路线（{verdict.status}），'
                         '整份证书须等待其证据补齐')
    for note in view.why_partial_fails:
        if not any(note in line for line in lines):
            lines.append(note)
    if not view.frozen:
        lines.append('组级验收按同批共享偏移整体判断，逐卷合格不等于整组合格')
    return list(dict.fromkeys(lines))
