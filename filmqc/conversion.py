from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .pairing import Pair, build_pairs
from .repository import Repository

REFERENCE = 'R0'
MIN_FIT_POINTS = 2


def _ols(points: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(points)
    x_mean = sum(x for x, _ in points) / n
    y_mean = sum(y for _, y in points) / n
    sxx = sum((x - x_mean) ** 2 for x, _ in points)
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in points)
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    return intercept, slope


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    series: str
    intercept: float
    slope: float
    fit_sheets: frozenset[str]
    fit_residuals: tuple[tuple[str, float], ...]
    holdout_sheets: frozenset[str]
    holdout_residuals: tuple[tuple[str, float], ...]
    fit_range: tuple[float, float]
    supported: bool
    note: str = ''

    @property
    def max_fit_error(self) -> float:
        return max((abs(r) for _, r in self.fit_residuals), default=math.inf)

    @property
    def holdout_error(self) -> float:
        """留出独立标准片上的最大绝对误差；无留出片则不可放行。"""
        if not self.holdout_residuals:
            return math.inf
        return max(abs(r) for _, r in self.holdout_residuals)

    def predict(self, value: float) -> float:
        return self.intercept + self.slope * value

    def in_fit_range(self, value: float) -> bool:
        return self.fit_range[0] <= value <= self.fit_range[1]


@dataclass(frozen=True)
class Route:
    """把某读头读数带到 R0 的一条有数据支持的路线。"""

    origin: str
    legs: tuple[str, ...]          # 例如 ('R2','R3','R0')
    relations: tuple[Relation, ...]
    direct: bool
    independent_holdouts: frozenset[str]
    holdout_error: float
    fit_sheet_union: frozenset[str]

    def predict(self, value: float) -> float:
        current = value
        for relation in self.relations:
            current = relation.predict(current)
        return current

    def applicable(self, value: float) -> bool:
        current = value
        for relation in self.relations:
            if not relation.in_fit_range(current):
                return False
            current = relation.predict(current)
        return True


def _purpose_partition(pairs: list[Pair], source: str, target: str) -> tuple[
        list[Pair], list[Pair]]:
    leg_pairs = [p for p in pairs
                 if {p.left_instrument, p.right_instrument} == {source, target}]
    fit, holdout = [], []
    for pair in leg_pairs:
        if pair.purposes <= {'fit'}:
            fit.append(pair)
        elif 'holdout' in pair.purposes:
            holdout.append(pair)
    return fit, holdout


def _xy(pair: Pair, source: str) -> tuple[float, float]:
    if pair.left_instrument == source:
        return pair.left_value, pair.right_value
    return pair.right_value, pair.left_value


def build_relations(repo: Repository, as_of: date,
                    pairs: list[Pair] | None = None) -> dict[tuple[str, str], Relation]:
    """为每个非参考读头建立到相邻读头的线性换算关系。

    关系的适用范围固定在拟合数据所在的材料系列与测量区间内，
    区间外不允许外推；相邻型号也不允许借关系。
    拟合片与留出片严格分开，互换组被撤销后其证据自动退出。
    """
    if pairs is None:
        pairs = build_pairs(repo, as_of)
    relations: dict[tuple[str, str], Relation] = {}
    instruments = [iid for iid in repo.instruments if iid != REFERENCE]
    for source in instruments:
        targets = {
            p.right_instrument if p.left_instrument == source
            else p.left_instrument
            for p in pairs
            if source in (p.left_instrument, p.right_instrument)
        }
        for target in sorted(targets):
            fit_pairs, holdout_pairs = _purpose_partition(pairs, source, target)
            series = repo.instruments[source].series
            if len(fit_pairs) < MIN_FIT_POINTS:
                relations[(source, target)] = Relation(
                    source, target, series, math.nan, math.nan,
                    frozenset(), (), frozenset(), (),
                    (math.nan, math.nan), False,
                    f'合格拟合片仅 {len(fit_pairs)} 片，少于 {MIN_FIT_POINTS} 片')
                continue
            points = [_xy(p, source) for p in fit_pairs]
            intercept, slope = _ols(points)
            fit_res = tuple(
                (p.sheet, y - (intercept + slope * x)) for p, (x, y)
                in zip(fit_pairs, points))
            holdout_res = tuple(
                (p.sheet, _xy(p, source)[1]
                 - (intercept + slope * _xy(p, source)[0]))
                for p in holdout_pairs)
            x_values = [x for x, _ in points]
            note = '' if holdout_res else '无独立留出标准片，不能用于放行'
            relations[(source, target)] = Relation(
                source, target, series, intercept, slope,
                frozenset(p.sheet for p in fit_pairs), fit_res,
                frozenset(p.sheet for p in holdout_pairs), holdout_res,
                (min(x_values), max(x_values)),
                supported=bool(holdout_res), note=note)
    return relations


@dataclass
class RouteEvaluation:
    route: Route
    predicted: float
    applicable: bool
    admissible: bool
    reasons: tuple[str, ...]
    budget_ok: bool
    error_budget: float | None = None  # 路线在订单允许总误差上的独立证据误差


def _chain_holdout(relations: dict[tuple[str, str], Relation],
                   path: tuple[str, ...]) -> tuple[frozenset[str], float, frozenset[str]]:
    """两次换算的独立留出片：必须在两段都有留出读数，且不属任一段拟合集。

    同一组标准片在两段同时出现只算一份独立证据，绝不重复计数。
    """
    first, second = relations[(path[0], path[1])], relations[(path[1], path[2])]
    shared = (first.holdout_sheets & second.holdout_sheets
              - first.fit_sheets - second.fit_sheets)
    union = first.fit_sheets | second.fit_sheets
    errors = []
    for sheet in shared:
        e1 = dict(first.holdout_residuals)[sheet]
        e2 = dict(second.holdout_residuals)[sheet]
        errors.append((sheet, abs(e1) + abs(e2)))  # 同读头偏移不假设相互抵消
    error = max((e for _, e in errors), default=math.inf)
    return frozenset(shared), error, union


def build_routes(repo: Repository, as_of: date,
                 relations: dict[tuple[str, str], Relation] | None = None
                 ) -> dict[str, list[Route]]:
    """枚举每个非参考读头到 R0 的直连路线与一次中转的两次换算路线。"""
    if relations is None:
        relations = build_relations(repo, as_of)
    routes: dict[str, list[Route]] = {}
    for origin in repo.instruments:
        if origin == REFERENCE:
            continue
        options: list[Route] = []
        direct = relations.get((origin, REFERENCE))
        if direct is not None and direct.supported:
            options.append(Route(
                origin, (origin, REFERENCE), (direct,), True,
                direct.holdout_sheets, direct.holdout_error,
                direct.fit_sheets))
        for (src, tgt), first in relations.items():
            if src != origin or tgt == REFERENCE or not first.supported:
                continue
            second = relations.get((tgt, REFERENCE))
            if second is None or not second.supported:
                continue
            path = (origin, tgt, REFERENCE)
            holdouts, error, fit_union = _chain_holdout(relations, path)
            if not holdouts:
                continue
            options.append(Route(origin, path, (first, second), False,
                                 holdouts, error, fit_union))
        routes[origin] = options
    return routes


def evaluate_route(route: Route, value: float, allowed_error: float) -> RouteEvaluation:
    applicable = route.applicable(value)
    reasons = []
    if not applicable:
        reasons.append('读数超出换算关系的拟合测量区间，禁止外推')
    if route.holdout_error > allowed_error:
        reasons.append(
            f'独立标准片总误差 {route.holdout_error:.3f} '
            f'超过订单允许总误差 {allowed_error:.3f}')
    admissible = applicable and not reasons
    return RouteEvaluation(route, route.predict(value), applicable,
                           admissible, tuple(reasons),
                           route.holdout_error <= allowed_error,
                           route.holdout_error)
