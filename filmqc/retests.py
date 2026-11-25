from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .repository import Repository

PENDING = 'pending'
DONE = 'done'


@dataclass(frozen=True)
class RetestItem:
    retest_id: str
    slice_id: str
    instrument: str
    owner: str
    slot: datetime
    state: str
    kind: str                       # roll_sampling 卷材取样 / standard 标准片补证
    covers_orders: tuple[str, ...]  # 卷材取样可覆盖的待办订单；标准片补证恒为空
    restores_certificates: tuple[str, ...]  # 标准片补证可恢复共同路径的证书
    duplicates: tuple[str, ...]     # 已存在的相同复测（避免重复占位）
    note: str


def _order_lot_index(repo: Repository) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for order in repo.orders.values():
        index.setdefault(order.lot, []).append(order.id)
    return index


def retest_board(repo: Repository, as_of: date) -> list[RetestItem]:
    """复测工作台：区分卷材取样与标准片补证，并标注可覆盖的待办。

    - order 检件上的复测是卷材取样，只能替代该采样许可下的证据；
    - fit/holdout 检件上的复测是标准片补证，可恢复共同换算路径，
      但不能冒充客户要求的实际取样；
    - 已有未完成（pending）的相同 (检件, 读头) 复测会显示其覆盖范围，
      新的相同申请按重复占位拒绝。
    """
    lot_orders = _order_lot_index(repo)
    pending_key: dict[tuple[str, str], list[str]] = {}
    for retest in repo.retests:
        if retest.state == PENDING:
            pending_key.setdefault((retest.slice, retest.instrument),
                                   []).append(retest.id)
    items: list[RetestItem] = []
    for retest in sorted(repo.retests, key=lambda r: r.slot):
        sl = repo.slices[retest.slice]
        duplicates = tuple(rid for rid
                           in pending_key.get((retest.slice, retest.instrument), [])
                           if rid != retest.id)
        if sl.purpose == 'order':
            kind = 'roll_sampling'
            restores: tuple[str, ...] = ()
            if retest.instrument == 'R0':
                note = ('R0 质控室复测仅可服务 lab_allowed=1 的同卷订单；'
                        'line_only 订单仍要求产线取样')
                covers = tuple(oid for oid in lot_orders.get(sl.lot, ())
                               if repo.orders[oid].lab_allowed)
            else:
                note = '产线读头复测，作为该卷材的产线取样证据'
                covers = tuple(lot_orders.get(sl.lot, ()))
        else:
            kind = 'standard'
            covers = ()
            restores = tuple(sorted(repo.certificates))
            note = ('标准片补证：可恢复相关证书的共同换算路径，'
                    '不能替代客户要求的卷材实际取样')
        if retest.state == DONE:
            note += '；该复测已完成，结果未进入 readings 前不产生新证据'
        items.append(RetestItem(retest.id, retest.slice, retest.instrument,
                                retest.owner, retest.slot, retest.state,
                                kind, covers, restores, duplicates, note))
    # 工作台只显示已到 slot 时点的记录；未完成申请在其 slot 当日起可见，
    # 与读数按 received 进入可见时点保持同一时间语义。
    return [item for item in items if item.slot.date() <= as_of]


def reject_duplicate_request(repo: Repository, slice_id: str,
                             instrument: str) -> str | None:
    """新复测申请若与未完成的相同 (检件, 读头) 复测撞车，返回占位编号。"""
    for retest in repo.retests:
        if (retest.slice == slice_id and retest.instrument == instrument
                and retest.state == PENDING):
            return retest.id
    return None
