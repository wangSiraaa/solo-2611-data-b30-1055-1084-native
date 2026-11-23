"""六队杯抽签包：排名快照 → 场馆目录 → 完签核对 → 公证抽签与重放。

对外入口
========
* :func:`build_catalog` / :func:`assert_catalog_matches_snapshot`
  —— 馆单与排名快照版本绑定。
* :func:`evaluate_pairing` / :func:`complete_draws`
  —— 核对完整配对、列实际可用馆与不可办依据；枚举全部完签。
* :func:`seat_opportunities` / :func:`consistent_orders`
  —— 给定已揭前缀，算各客队还剩多少种完签。
* :func:`make_draw` / :func:`replay_draw` / :func:`replay_from_draws`
  —— 均匀整数抽签、封存凭证、独立重放。
* :func:`seat_state_after_prefix` —— 停电后接班，确认已生效抽数、不换号重摇。
* :func:`recount_integrity` —— 换馆不重复入册的票本清点。
"""

from .model import (
    Catalog,
    CatalogError,
    Venue,
    assert_catalog_matches_snapshot,
    build_catalog,
    complete_draws,
    consistent_orders,
    count_venue_arrangements,
    enumerate_away_orders,
    evaluate_pairing,
    match_venues,
    recount_integrity,
    seat_opportunities,
)
from .draw import (
    DrawError,
    UniformSource,
    away_index,
    make_draw,
    replay_draw,
    replay_from_draws,
    seat_state_after_prefix,
    uniform_numbers,
)

__all__ = [
    "Catalog", "CatalogError", "Venue", "DrawError", "UniformSource",
    "build_catalog", "assert_catalog_matches_snapshot", "evaluate_pairing",
    "complete_draws", "enumerate_away_orders", "consistent_orders",
    "seat_opportunities", "count_venue_arrangements", "match_venues",
    "recount_integrity", "make_draw", "replay_draw", "replay_from_draws",
    "seat_state_after_prefix", "uniform_numbers", "away_index",
]
