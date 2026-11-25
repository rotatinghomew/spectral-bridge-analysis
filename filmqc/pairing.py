from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .repository import Repository


def swap_active(repo: Repository, swap: str, as_of: date) -> bool:
    """互换组在 as_of 时点是否仍有效；空白表示从未确认，视为无效。"""
    if not swap:
        return False
    return swap not in repo.withdrawn_swaps(as_of)


def observed_means(repo: Repository, as_of: date) -> dict[tuple[str, str], float]:
    """返回 (检件, 读头) 的可见平均读数；重复读只生成一个检件级数值。"""
    bucket: dict[tuple[str, str], list[float]] = defaultdict(list)
    for reading in repo.visible_readings(as_of):
        if not repo.instrument_measurement(reading):
            continue
        bucket[(reading.slice, reading.instrument)].append(reading.value)
    return {key: sum(values) / len(values) for key, values in bucket.items()}


@dataclass(frozen=True)
class Pair:
    """同一物理片在两台读头上的一组可配对证据。"""

    sheet: str
    left_instrument: str
    right_instrument: str
    left_value: float
    right_value: float
    purposes: frozenset[str]
    left_slices: frozenset[str] = field(default_factory=frozenset)
    right_slices: frozenset[str] = field(default_factory=frozenset)
    paired_by: str = 'sheet'  # sheet=同检件；swap=确认仍有效的互换组


def _slice_values(repo: Repository, means, instrument: str) -> dict[str, float]:
    return {slice_id: value for (slice_id, inst), value in means.items()
            if inst == instrument}


def build_pairs(repo: Repository, as_of: date) -> list[Pair]:
    """按物理片汇总跨读头配对。

    配对条件（任一满足）：
    1. 同一检件被两台读头重复读；
    2. 两个检件来自同一物理片且显式记录的互换组在 as_of 仍有效。
    撤销的互换声明不删除读数，只禁止据此配对。
    """
    means = observed_means(repo, as_of)
    pairs: dict[tuple[str, str, str], Pair] = {}
    for sheet, slice_ids in repo._slices_by_sheet.items():
        per_instrument: dict[str, dict[str, float]] = defaultdict(dict)
        for slice_id in slice_ids:
            for (sid, inst), value in means.items():
                if sid == slice_id:
                    per_instrument[inst][slice_id] = value
        instruments = sorted(per_instrument)
        for index, left in enumerate(instruments):
            for right in instruments[index + 1:]:
                left_map, right_map = per_instrument[left], per_instrument[right]
                # 同检件配对
                common = sorted(set(left_map) & set(right_map))
                if common:
                    lv = sum(left_map[s] for s in common) / len(common)
                    rv = sum(right_map[s] for s in common) / len(common)
                    purposes = frozenset(repo.slices[s].purpose for s in common)
                    key = (sheet, left, right)
                    pairs[key] = Pair(sheet, left, right, lv, rv, purposes,
                                      frozenset(common), frozenset(common),
                                      'sheet')
                    continue
                # 互换组配对：该物理片上全部检件共享同一 swap 时才允许
                swaps = {repo.slices[s].swap for s in slice_ids
                         if s in left_map or s in right_map}
                if len(swaps) == 1 and swap_active(repo, next(iter(swaps)), as_of):
                    left_slices = frozenset(left_map)
                    right_slices = frozenset(right_map)
                    if not left_slices or not right_slices:
                        continue
                    lv = sum(left_map.values()) / len(left_map)
                    rv = sum(right_map.values()) / len(right_map)
                    purposes = frozenset(repo.slices[s].purpose
                                         for s in left_slices | right_slices)
                    pairs[(sheet, left, right)] = Pair(
                        sheet, left, right, lv, rv, purposes,
                        left_slices, right_slices, 'swap')
    return sorted(pairs.values(), key=lambda p: (p.sheet, p.left_instrument,
                                                 p.right_instrument))
