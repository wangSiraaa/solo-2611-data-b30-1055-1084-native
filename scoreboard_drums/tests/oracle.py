"""独立第二实现：用最直白的 minimax 暴力枚举策略深度，交叉验证 planner。

与 scoreboard.planner 的实现完全独立：不共享信念更新函数、不共享记忆化，
直接在 (A,B) 数字元组集合上递归，枚举“走 A/走 B/读 A/读 B”全部策略，
返回能保证到达目标的最小最坏拍数；horizon 内无解返回 None。
"""

from __future__ import annotations


def _fwd(d: int) -> int:
    return (d + 1) % 10


def brute_min_beats(stops: frozenset[tuple[int, int]],
                    target: tuple[int, int], horizon: int) -> int | None:
    def rec(belief: frozenset, left: int) -> int | None:
        if all(s == target for s in belief):
            return 0
        if left == 0:
            return None
        candidates = []

        # 走格：两鼓各试一次（对已全部到位的鼓无意义，跳过）
        for idx in (0, 1):
            if any(s[idx] != target[idx] for s in belief):
                moved = frozenset(
                    tuple(_fwd(d) if i == idx else d for i, d in enumerate(s))
                    for s in belief)
                sub = rec(moved, left - 1)
                if sub is not None:
                    candidates.append(1 + sub)

        # 读头：按读数分叉，深度取分支最大值
        for idx in (0, 1):
            readings = {s[idx] for s in belief}
            if len(readings) > 1:
                worst = 0
                ok = True
                for digit in readings:
                    branch = frozenset(s for s in belief if s[idx] == digit)
                    sub = rec(branch, left - 1)
                    if sub is None:
                        ok = False
                        break
                    worst = max(worst, 1 + sub)
                if ok:
                    candidates.append(worst)

        return min(candidates) if candidates else None

    return rec(stops, horizon)
