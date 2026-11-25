from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path

from filmqc import (
    Repository,
    build_pairs,
    build_relations,
    build_routes,
    observed_means,
    swap_active,
)
from filmqc.conversion import REFERENCE, evaluate_route
from filmqc.decisions import (
    DISPUTE,
    DISPUTE_BUDGET,
    HOLD,
    PASS,
    RETEST,
    assess_rolls,
    certificate_view,
    partial_subset,
)
from filmqc.retests import reject_duplicate_request, retest_board

DATA = Path(__file__).resolve().parents[1] / 'data'
AS_OF = date(2026, 4, 10)
BEFORE_WITHDRAW = date(2026, 4, 6)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(DATA)

    def test_evidence_is_keyed_by_physical_sheet(self):
        pairs = build_pairs(self.repo, AS_OF)
        sheets = {p.sheet for p in pairs}
        # 配对结果以物理片为单位，同片重复读不产生多份证据
        self.assertIn('P01', sheets)
        s01_pairs = [p for p in pairs if p.sheet == 'P01']
        self.assertTrue(all(p.paired_by == 'sheet' for p in s01_pairs))

    def test_withdrawn_swap_removes_pair_but_keeps_readings(self):
        means = observed_means(self.repo, AS_OF)
        # 撤销不删除原始读数：S04B 的 R2/R3 读数仍可见
        self.assertIn(('S04B', 'R2'), means)
        self.assertIn(('S04B', 'R3'), means)
        self.assertFalse(swap_active(self.repo, 'G04', AS_OF))
        pairs_now = build_pairs(self.repo, AS_OF)
        # 撤销后不得再出现经互换组的配对；S04B 同检件 R2/R3 直读配对仍合法
        self.assertFalse(any(p.paired_by == 'swap' and p.sheet == 'P04'
                             for p in pairs_now))
        self.assertTrue(any(p.paired_by == 'sheet' and p.sheet == 'P04'
                            for p in pairs_now))
        # 撤销前（04-06）G04 仍有效，S04/S04B 经互换组可配对
        self.assertTrue(swap_active(self.repo, 'G04', BEFORE_WITHDRAW))
        pairs_then = build_pairs(self.repo, BEFORE_WITHDRAW)
        p04_then = [p for p in pairs_then if p.sheet == 'P04']
        self.assertTrue(any(p.paired_by == 'swap' for p in p04_then))

    def test_repeated_readings_average_to_one_slice_value(self):
        means = observed_means(self.repo, AS_OF)
        self.assertAlmostEqual(means[('S04B', 'R3')], (39.4 + 39.2) / 2)

    def test_unconfirmed_swap_never_pairs(self):
        # S09 的 swap 为空，即使同批存在也不得与其他检件配对
        pairs = build_pairs(self.repo, AS_OF)
        self.assertFalse(any(p.sheet == 'P09' for p in pairs))


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(DATA)

    def test_relations_partition_fit_and_holdout_sheets(self):
        relations = build_relations(self.repo, AS_OF)
        direct = relations[('R2', REFERENCE)]
        self.assertTrue(direct.supported)
        self.assertEqual(direct.fit_sheets, {'P01', 'P02', 'P03'})
        self.assertEqual(direct.holdout_sheets, {'P05'})  # P04 随 G04 撤销退出
        self.assertTrue(direct.fit_sheets.isdisjoint(direct.holdout_sheets))
        self.assertAlmostEqual(direct.holdout_error, 0.351, places=2)

    def test_unsupported_relation_when_too_few_fit_sheets(self):
        relations = build_relations(self.repo, AS_OF)
        r1 = relations[('R1', REFERENCE)]
        self.assertFalse(r1.supported)
        self.assertIn('少于 2 片', r1.note)

    def test_chain_counts_shared_standard_once(self):
        routes = build_routes(self.repo, AS_OF)
        chain = next(r for r in routes['R2'] if not r.direct)
        self.assertEqual(chain.legs, ('R2', 'R3', REFERENCE))
        # P05 是唯一在两段都独立于拟合集的留出片，只计一份
        self.assertEqual(chain.independent_holdouts, frozenset({'P05'}))
        self.assertAlmostEqual(chain.holdout_error, 1.354, places=2)

    def test_extrapolation_is_rejected(self):
        routes = build_routes(self.repo, AS_OF)
        direct = next(r for r in routes['R2'] if r.direct)
        # 订单允许误差给得很宽，排除预算因素；区间外读数必须判不可应用
        evaluation = evaluate_route(direct, 95.0, allowed_error=100.0)
        self.assertFalse(evaluation.applicable)
        self.assertTrue(any('外推' in reason for reason in evaluation.reasons))

    def test_withdrawal_shrinks_holdout_evidence_over_time(self):
        before = build_relations(self.repo, BEFORE_WITHDRAW)
        after = build_relations(self.repo, AS_OF)
        self.assertEqual(before[('R2', REFERENCE)].holdout_sheets, {'P04', 'P05'})
        self.assertEqual(after[('R2', REFERENCE)].holdout_sheets, {'P05'})

    def test_visibility_follows_received_timestamp(self):
        # S08 的读数 04-05 测量、04-09 才到达：04-06 尚不可见
        early = observed_means(self.repo, BEFORE_WITHDRAW)
        self.assertNotIn(('S08', 'R2'), early)
        visible = observed_means(self.repo, AS_OF)
        self.assertIn(('S08', 'R2'), visible)
        # S08 只参与 R2->R3 段，没有 R0，不能冒充全链独立证据
        routes = build_routes(self.repo, AS_OF)
        chain = next(r for r in routes['R2'] if not r.direct)
        self.assertNotIn('P08', chain.independent_holdouts)

    def test_instrument_window_and_series_govern_evidence(self):
        # R2 的实际测量区间为 20~85：区间外读数不进入证据
        inst = self.repo.instruments['R2']
        self.assertFalse(inst.in_window(19.9))
        self.assertTrue(inst.in_window(20.0))
        self.assertTrue(inst.in_window(85.0))
        # R2 在 2026-04-01 才生效，旧读数不能挂到新读头
        self.assertFalse(inst.covers_day(date(2026, 3, 31)))
        self.assertTrue(inst.covers_day(date(2026, 4, 1)))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(DATA)
        self.rolls = assess_rolls(self.repo, AS_OF)
        self.verdicts = {oid: verdict for roll in self.rolls
                         for oid, verdict in roll.order_verdicts.items()}

    def test_clean_rolls_pass_and_direct_r0_is_accepted(self):
        # L02 只有 R0 直读，O2 lab_allowed=1，直接沿用
        self.assertEqual(self.verdicts['O2'].status, PASS)
        self.assertEqual(self.verdicts['O2'].evidence_errors, (0.0,))
        # O3 两条路线都在 1.4 预算内且都判合格
        self.assertEqual(self.verdicts['O3'].status, PASS)

    def test_budget_conflict_is_dispute_not_favorable_pick(self):
        for oid in ('O1', 'O4', 'O5', 'O6'):
            verdict = self.verdicts[oid]
            self.assertEqual(verdict.status, DISPUTE, oid)
            self.assertEqual(verdict.dispute_kind, DISPUTE_BUDGET)
            self.assertTrue(any('主要误差来源' in r for r in verdict.reasons))

    def test_line_only_rejects_qc_retest_but_keeps_line_evidence(self):
        o1 = self.verdicts['O1']
        self.assertTrue(any('line_only 订单不可用' in r
                            for r in o1.remediation))
        # 产线 R2 换算路线仍作为产线证据参与判定
        self.assertTrue(any(e.route.origin == 'R2' for e in o1.routes))
        self.assertNotIn(0.0, o1.evidence_errors)

    def test_lab_retest_remediation_only_for_eligible_order(self):
        o6 = self.verdicts['O6']
        self.assertTrue(any('T1' in r and 'R0 卷材取样' in r
                            for r in o6.remediation))
        o1 = self.verdicts['O1']
        self.assertTrue(any('line_only 订单不可用' in r for r in o1.remediation))

    def test_roll_without_evidence_and_without_retest_is_held(self):
        # 构造一个没有任何可见读数的极端时点不应崩溃，且无证据判 hold
        rolls = assess_rolls(self.repo, date(2026, 1, 2))
        statuses = {v.status for roll in rolls
                    for v in roll.order_verdicts.values()}
        self.assertIn(HOLD, statuses)


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(DATA)
        self.views = {view.certificate_id: view
                      for view in certificate_view(self.repo, AS_OF)}

    def test_group_uses_shared_bias_mean_not_independent_average(self):
        c2 = self.views['C2']
        # O2=0.0（R0 直读），其余争议订单只有直连预算内误差 0.351
        self.assertTrue(c2.group_pass)
        self.assertAlmostEqual(c2.group_values[0], 0.234, places=2)

    def test_full_certificate_blocked_when_any_member_disputed(self):
        c1 = self.views['C1']
        self.assertEqual(c1.issue_status, 'blocked')  # partial_allowed=0
        self.assertTrue(any('O1' in line for line in
                            [x for x in self._lines(c1)]))

    @staticmethod
    def _lines(view):
        from filmqc.decisions import explain_hold
        return explain_hold(view)

    def test_partial_delivery_keeps_only_group_compliant_subset(self):
        c2 = self.views['C2']
        self.assertEqual(c2.issue_status, 'partial')
        subset = partial_subset(self.repo, c2)
        self.assertEqual(subset, ('O2',))
        c1 = self.views['C1']
        self.assertEqual(partial_subset(self.repo, c1), ())

    def test_issued_certificate_is_frozen_with_impact_note_only(self):
        c3 = self.views['C3']
        self.assertTrue(c3.frozen)
        self.assertEqual(c3.issue_status, 'frozen')
        self.assertTrue(c3.group_pass)  # 历史采用证据下仍满足 1.1
        self.assertTrue(c3.impact_note)
        kinds = {change['type'] for change in c3.impact_changes}
        self.assertIn('swap_withdrawn', kinds)
        self.assertIn('holdout_changed', kinds)
        # 卷宗保留签发时点快照
        self.assertEqual(c3.dossier['as_of'], '2026-04-06')

    def test_dossier_traces_readings_relations_and_retests_path(self):
        c1 = self.views['C1']
        entry = c1.dossier['orders']['O1']
        self.assertEqual(entry['lot'], 'L01')
        legs = {tuple(route['legs']) for route in entry['routes']}
        self.assertIn(('R2', 'R0'), legs)
        for route in entry['routes']:
            for relation in route['relations']:
                self.assertTrue(relation['fit_sheets'])
                self.assertTrue(relation['fit_range'])


class RetestBoardTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(DATA)
        self.board = {item.retest_id: item
                      for item in retest_board(self.repo, AS_OF)}

    def test_roll_sampling_vs_standard_distinction(self):
        t1 = self.board['T1']
        self.assertEqual(t1.kind, 'roll_sampling')
        self.assertEqual(t1.covers_orders, ('O6',))  # O1 line_only 被排除
        t3 = self.board['T3']
        self.assertEqual(t3.kind, 'standard')
        self.assertEqual(t3.covers_orders, ())      # 标准片不能冒充取样
        self.assertIn('C2', t3.restores_certificates)

    def test_duplicate_pending_request_is_rejected(self):
        self.assertEqual(reject_duplicate_request(self.repo, 'S09', 'R0'), 'T1')
        self.assertIsNone(reject_duplicate_request(self.repo, 'S09', 'R2'))

    def test_done_retest_without_reading_creates_no_evidence(self):
        t2 = self.board['T2']
        self.assertEqual(t2.state, 'done')
        self.assertIn('未进入 readings 前不产生新证据', t2.note)
        means = observed_means(self.repo, AS_OF)
        self.assertNotIn(('S06', 'R2'), means)


if __name__ == '__main__':
    unittest.main()
