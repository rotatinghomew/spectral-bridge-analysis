"""薄膜分光读数跨尺度可比较性与出货判定。

模块只依赖标准库；所有判断都以物理片（sheet）为独立证据单位，
读数与复测申请均按收到时间（received / slot）进入可见时点。
"""

from .repository import Repository
from .pairing import Pair, build_pairs, observed_means, swap_active
from .conversion import (
    Relation,
    Route,
    build_relations,
    build_routes,
    evaluate_route,
)
from .decisions import (
    OrderVerdict,
    RollAssessment,
    assess_rolls,
    certificate_view,
    explain_hold,
)
from .retests import retest_board, RetestItem

__all__ = [
    'Repository',
    'Pair',
    'build_pairs',
    'observed_means',
    'swap_active',
    'Relation',
    'Route',
    'build_relations',
    'build_routes',
    'evaluate_route',
    'OrderVerdict',
    'RollAssessment',
    'assess_rolls',
    'certificate_view',
    'explain_hold',
    'retest_board',
    'RetestItem',
]
