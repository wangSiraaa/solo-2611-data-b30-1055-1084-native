"""河岸手标本：原位朝向、切面关系与具名盒配发的约束核。

本模块只做三件事，且严格按题目规则，不做任何额外推断：

1. 把水平四向（顺岸/背岸/上坡/下坡）编码成 Z4，把野外照片归约为两类
   约束：某槽允许的绝对朝向集合、两槽之间相差的四分之一周格数集合。
   枚举整组照片同时成立的全部现场朝向解释；解释集为空即照片相抵，
   并回报互相打架的原始约束。
2. 每件方料只有一块料、只够一次入盒切割；夹具模式决定它只能沿槽面
   （槽向格）还是只能沿邻面（槽向 + 1 格），或两面择一。切面只看
   格数奇偶：同向/反向（相差 0 或 2 格）为平行，相差 1 或 3 格为正交。
3. 一个具名盒要两件不同实物（一红一灰等），其平行/正交关系必须在
   *全部*可行原位解释下都成立才允许发出；归还在途的片不能再次装盒。
   在小料规模下用枚举求“最多整盒 + 实际切单”，数量并列时任选其一。

设计上把“朝向事实”与“配发决策”分开：:class:`OrientationCase` 是纯
Z4 约束问题，:class:`Block` / :class:`LoanBox` / :func:`allocate` 是
配发问题，配发只接受朝向问题已经给出的完整解释集，绝不偷看某个单解。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations, product
from typing import Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# 方向编码：展方给定的四分之一周顺序 0,1,2,3。
# 相反两向相差 2 格，属同一切面方向，所以切面关系只由 mod 2 决定。
# ---------------------------------------------------------------------------

ALONG_SHORE = 0  # 顺岸
INLAND = 1       # 背岸
UP_SLOPE = 2     # 上坡
DOWN_SLOPE = 3   # 下坡

DIRECTION_NAME = ("顺岸", "背岸", "上坡", "下坡")


def mod4(n: int) -> int:
    return n % 4


def plane_relation(plane_a: int, plane_b: int) -> str:
    """两个切面向（格数）的原地关系：平行或正交。"""
    return "平行" if (plane_a - plane_b) % 2 == 0 else "正交"


class FaceMode(str, Enum):
    """夹具允许下刀的本地面。"""

    SLOT_ONLY = "slot_only"          # 只能沿有槽面切
    ADJACENT_ONLY = "adjacent_only"  # 只能沿槽的邻面切
    EITHER = "either"                # 槽面或邻面择一

    @property
    def offset(self) -> int:
        """相对槽向顺转的格数：槽面 0、邻面 1。"""
        return 0 if self is FaceMode.SLOT_ONLY else 1


@dataclass(frozen=True)
class BearingConstraint:
    """“某槽可能朝哪几向”：block 的槽向必须落在 bearings 内。"""

    block: str
    bearings: Tuple[int, ...]

    def satisfied(self, bearing: int) -> bool:
        return bearing in self.bearings


@dataclass(frozen=True)
class TurnConstraint:
    """“乙槽比甲槽顺转几格”：bearing(b) - bearing(a) ∈ turns (mod 4)。"""

    block_a: str
    block_b: str
    turns: Tuple[int, ...]

    def satisfied(self, bearing_a: int, bearing_b: int) -> bool:
        return mod4(bearing_b - bearing_a) in self.turns


@dataclass
class OrientationCase:
    """一组照片对应的完整朝向问题。"""

    blocks: Tuple[str, ...]
    bearing_constraints: List[BearingConstraint] = field(default_factory=list)
    turn_constraints: List[TurnConstraint] = field(default_factory=list)

    def feasible_assignments(self) -> List[Dict[str, int]]:
        """枚举满足整组照片的全部现场朝向（每料 4 向，小规模直接枚举）。"""
        out: List[Dict[str, int]] = []
        for values in product(range(4), repeat=len(self.blocks)):
            assignment = dict(zip(self.blocks, values))
            if all(
                c.satisfied(assignment[c.block]) for c in self.bearing_constraints
            ) and all(
                c.satisfied(assignment[c.block_a], assignment[c.block_b])
                for c in self.turn_constraints
            ):
                out.append(assignment)
        return out

    def conflicting_constraints(
        self,
    ) -> Tuple[List[BearingConstraint], List[TurnConstraint]]:
        """照片相抵时，逐条找出在任何朝向下都无法参与可行解的原件。

        判定口径：去掉该条后若存在可行解、而带上它则无解，它就是相抵件。
        多条同时互相矛盾时会全部报出，不人为指定“谁错了”；若没有任何
        单条可被摘出来救活问题，则属联合矛盾，整组原件一并回报。
        """
        if self.feasible_assignments():
            return [], []

        def revive(bearings, turns) -> bool:
            return bool(
                OrientationCase(self.blocks, list(bearings), list(turns))
                .feasible_assignments()
            )

        bad_bearings = [
            c
            for c in self.bearing_constraints
            if revive(
                [x for x in self.bearing_constraints if x != c],
                self.turn_constraints,
            )
        ]
        bad_turns = [
            c
            for c in self.turn_constraints
            if revive(
                self.bearing_constraints,
                [x for x in self.turn_constraints if x != c],
            )
        ]
        if not bad_bearings and not bad_turns:
            return list(self.bearing_constraints), list(self.turn_constraints)
        return bad_bearings, bad_turns


# ---------------------------------------------------------------------------
# 方料、切面与盒
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """一件具名方料。每个名字代表唯一实物，重号不合并（旧巡检核验在外）。"""

    name: str
    color: str                 # 红 / 灰 …
    pattern: str               # 色纹种类
    source: str                # 明订产区（同一处沿岸岩面）
    face_mode: FaceMode
    thickness: int = 1         # 本体还够几次实质下刀/磨面的工单厚度
    in_transit: bool = False   # 归还在途：其片不能再次装盒

    def available_modes(self) -> Tuple[FaceMode, ...]:
        if self.in_transit:
            return ()
        if self.face_mode is FaceMode.EITHER:
            return (FaceMode.SLOT_ONLY, FaceMode.ADJACENT_ONLY)
        return (self.face_mode,)


def plane_for_cut(bearing: int, cut_face: FaceMode) -> int:
    """槽向 + 下刀面 => 切面向（Z4 格）。邻面比槽面顺转一格。"""
    return mod4(bearing + cut_face.offset)


@dataclass(frozen=True)
class Cut:
    """一次已发生的切割。已胶封后方向不可改，料随截面转出。"""

    block: str
    plane: int
    face: FaceMode


@dataclass(frozen=True)
class CutOption:
    """在某个朝向解释下，某料可取的一种下刀方案。"""

    block: str
    face: FaceMode
    plane: int


@dataclass
class LoanBox:
    """一具名借展盒的教材关系。"""

    name: str
    colors: Tuple[str, ...]
    required_relation: str          # "平行" / "正交"
    source: Optional[str] = None    # 限定产区；None 表示不限
    pattern: Optional[str] = None   # 限定色纹；None 表示不限

    def accepts(self, block_a: "Block", block_b: "Block") -> bool:
        if block_a.name == block_b.name or block_a.in_transit or block_b.in_transit:
            return False
        if sorted((block_a.color, block_b.color)) != sorted(self.colors):
            return False
        if len(set(self.colors)) != len(self.colors):
            return False  # 盒要两件不同实物/不同色，重复色需求本身非法
        if self.source is not None and (
            block_a.source != self.source or block_b.source != self.source
        ):
            return False
        if self.pattern is not None and (
            block_a.pattern != self.pattern or block_b.pattern != self.pattern
        ):
            return False
        return True


def block_cut_options(
    block: Block, assignment: Dict[str, int]
) -> Tuple[CutOption, ...]:
    """某一现场朝向下，该料按夹具模式能切出的全部方案。"""
    if block.in_transit:
        return ()
    bearing = assignment[block.name]
    return tuple(
        CutOption(block.name, mode, plane_for_cut(bearing, mode))
        for mode in block.available_modes()
    )


def robust_face_pairs(
    block_a: Block,
    block_b: Block,
    assignments: Sequence[Dict[str, int]],
) -> List[Tuple[FaceMode, FaceMode, str]]:
    """在全部可行解释下关系都恒定的 (A面, B面, 关系) 组合。

    固定两个本地面后，每解释下的关系由槽向差奇偶与面偏移奇偶共同决定；
    只有所有解释给出同一关系，该面组合才算稳健，才可入盒。
    """
    if not assignments:
        return []
    modes_a = block_a.available_modes()
    modes_b = block_b.available_modes()
    out: List[Tuple[FaceMode, FaceMode, str]] = []
    for mode_a in modes_a:
        for mode_b in modes_b:
            relations = {
                plane_relation(
                    plane_for_cut(assignment[block_a.name], mode_a),
                    plane_for_cut(assignment[block_b.name], mode_b),
                )
                for assignment in assignments
            }
            if len(relations) == 1:
                out.append((mode_a, mode_b, next(iter(relations))))
    return out


def robust_pair_relation(
    block_a: Block,
    block_b: Block,
    assignments: Sequence[Dict[str, int]],
) -> Optional[str]:
    """两件在全部可行原位解释下能否给出唯一共同关系。"""
    face_pairs = robust_face_pairs(block_a, block_b, assignments)
    relations = {r for _, _, r in face_pairs}
    if len(relations) == 1:
        return next(iter(relations))
    return None


@dataclass
class BoxFill:
    box: LoanBox
    block_a: Block
    block_b: Block
    face_a: FaceMode
    face_b: FaceMode
    cuts: Tuple[Cut, ...]
    relation: str


@dataclass
class AllocationResult:
    fills: List[BoxFill]
    unissued: List[str]
    cuts: Dict[str, Cut]

    @property
    def box_count(self) -> int:
        return len(self.fills)


def _sealed_compatible(
    block: Block,
    face: FaceMode,
    assignments: Sequence[Dict[str, int]],
    sealed: Optional[Cut],
) -> bool:
    """已胶封料只能按现有截面随料转出。

    胶封锁定的是 *本地面*（槽面/邻面）这一实物事实；同一现场截面在不同
    可行朝向解释下的 Z4 格数会整体平移，所以这里不比较格数字面量。
    关系是否在全部解释下成立，由 :func:`robust_face_pairs` 另行保证。
    """
    if sealed is None:
        return True
    return sealed.face == face


def allocate(
    boxes: Sequence[LoanBox],
    blocks: Sequence[Block],
    assignments: Sequence[Dict[str, int]],
    existing_cuts: Optional[Dict[str, Cut]] = None,
) -> AllocationResult:
    """求最多可同时发出的整盒与实际切单。

    规则落点：
      * 一盒两件不同实物，关系在全部可行原位解释下都满足教材要求；
      * 一件料最多入一盒、只切一次；已切胶封的方向不可改，只能连同
        现有截面转出；
      * 在途归还片直接不可用（:meth:`Block.available_modes` 为空）；
      * 最大化发出盒数；并列时任选第一组最大解，并给出未发盒清单。
    """
    if not assignments:
        raise ValueError("朝向解释集为空：先处理相抵照片，不能配发")

    cuts: Dict[str, Cut] = dict(existing_cuts or {})

    # 每盒的候选：盒规接受 + 存在稳健面组合 + 与已封切相容。
    candidates: Dict[str, List[Tuple[Block, Block, FaceMode, FaceMode, str]]] = {}
    for box in boxes:
        cand = []
        for a, b in combinations(blocks, 2):
            if not box.accepts(a, b):
                continue
            for mode_a, mode_b, relation in robust_face_pairs(a, b, assignments):
                if relation != box.required_relation:
                    continue
                if not _sealed_compatible(
                    a, mode_a, assignments, cuts.get(a.name)
                ) or not _sealed_compatible(
                    b, mode_b, assignments, cuts.get(b.name)
                ):
                    continue
                # 已胶封料连截面一起锁定。robust_face_pairs 只保证“这个
                # 面组合”关系恒定，但搭档可能靠换面凑出另一种关系；这里
                # 用封切料的实际胶封面，逐解释核对它与候选面给出的关系。
                sealed_pair_ok = True
                sealed_checks = (
                    (a, cuts.get(a.name), b, mode_b),
                    (b, cuts.get(b.name), a, mode_a),
                )
                for sealed_block, sealed_cut, other_block, other_face in sealed_checks:
                    if sealed_cut is None:
                        continue
                    locked = {
                        plane_relation(
                            plane_for_cut(
                                assignment[sealed_block.name], sealed_cut.face
                            ),
                            plane_for_cut(
                                assignment[other_block.name], other_face
                            ),
                        )
                        for assignment in assignments
                    }
                    if locked != {box.required_relation}:
                        sealed_pair_ok = False
                        break
                if not sealed_pair_ok:
                    continue
                cand.append((a, b, mode_a, mode_b, relation))
        candidates[box.name] = cand

    chosen: Dict[str, Tuple[Block, Block, FaceMode, FaceMode, str]] = {}
    used: Set[str] = set()

    def search(index: int) -> bool:
        if index == len(boxes):
            return True
        box = boxes[index]
        for a, b, mode_a, mode_b, relation in candidates[box.name]:
            if a.name in used or b.name in used:
                continue
            used.update((a.name, b.name))
            chosen[box.name] = (a, b, mode_a, mode_b, relation)
            if search(index + 1):
                return True
            used.difference_update((a.name, b.name))
            chosen.pop(box.name, None)
        return search(index + 1)  # 该盒发不出可跳过：少发优于错标

    search(0)

    fills: List[BoxFill] = []
    for box in boxes:
        if box.name not in chosen:
            continue
        a, b, mode_a, mode_b, relation = chosen[box.name]
        box_cuts: List[Cut] = []
        for block, mode in ((a, mode_a), (b, mode_b)):
            sealed = cuts.get(block.name)
            if sealed is not None:
                box_cuts.append(sealed)
                continue
            plane = plane_for_cut(assignments[0][block.name], mode)
            cut = Cut(block=block.name, plane=plane, face=mode)
            cuts[block.name] = cut
            box_cuts.append(cut)
        fills.append(
            BoxFill(
                box=box,
                block_a=a,
                block_b=b,
                face_a=mode_a,
                face_b=mode_b,
                cuts=tuple(box_cuts),
                relation=relation,
            )
        )

    unissued = [box.name for box in boxes if box.name not in chosen]
    return AllocationResult(fills=fills, unissued=unissued, cuts=cuts)
