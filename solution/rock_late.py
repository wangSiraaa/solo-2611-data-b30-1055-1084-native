"""晚到借展盒的两种订补处理策略。

题目对晚单的约束拆成两条硬规则：

* 首刀之前可以把一份具名方料 *封给* 某具名盒；占用后即使展方暂不到，
  也不能再把同一料改封给另一盒。先切好的料只能连同现有截面转出，
  不能重新锯成另一方向。
* 双台定位架做“试纹”：先磨下一面的一层薄屑读纹，该可用面与剩余厚度
  按工单扣掉；定向后的本体只够再切一次。因此试纹是真实损耗，不是
  免费的信息。

关键认知限制：选择处理方式时（首刀前）还不知道晚盒是否出现、需要平行
还是正交。所以这里实现的是 *策略*——在给定参数下对两种可能晚单都安全
的动作——而不是偷看晚单内容再回头优化。

状态口径
--------
* ``reserved_for`` 非空的料对早盒和其他晚盒都不可见（硬占用）。
* 早盒只在自由料之间配发；晚盒只在“封给它的料 + 发完早盒后仍自由的
  料”之间配发。
* 已胶封 :class:`Cut` 的料没有“再切一次”，只能按现有截面验证关系。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from rock_studio import (
    Block,
    Cut,
    FaceMode,
    LoanBox,
    allocate,
    robust_face_pairs,
)


class LateOutcome(str, Enum):
    NOT_ARRIVED = "not_arrived"
    PARALLEL = "平行"
    ORTHOGONAL = "正交"


@dataclass
class LateOrderSpec:
    """晚单工单：可能不到，到则关系二选一；另有损耗与归还间隔上限。"""

    box_name: str
    colors: Tuple[str, ...]
    possible_relations: Tuple[str, ...]
    allowed_loss: int            # 允许磨掉的厚度层数
    max_return_gap: int          # 最长归还间隔（在途超时则候补失效）
    source: Optional[str] = None
    pattern: Optional[str] = None

    def return_within_gap(self, actual_gap: Optional[int]) -> bool:
        """在途片是否赶得上晚单。None 表示尚未归还，按超时不可用处理。"""
        if actual_gap is None:
            return False
        return actual_gap <= self.max_return_gap


@dataclass
class BlockState:
    """方料随工单推进的实物状态。"""

    block: Block
    thickness: int
    test_ground_faces: Tuple[FaceMode, ...] = ()
    sealed_cut: Optional[Cut] = None
    reserved_for: Optional[str] = None

    def is_free(self) -> bool:
        return self.reserved_for is None and not self.block.in_transit

    def remaining_modes(self) -> Tuple[FaceMode, ...]:
        """扣除在途、已磨废面、已封切面之后仍可下刀的本地面。"""
        if self.block.in_transit:
            return ()
        modes = self.block.available_modes()
        if self.sealed_cut is not None:
            return (self.sealed_cut.face,)  # 胶封后只能沿用原截面
        return tuple(m for m in modes if m not in self.test_ground_faces)


@dataclass
class PolicyVerdict:
    outcome: LateOutcome
    issued: List[str]
    refused_late: bool
    reason: str


class _PolicyBase:
    def __init__(
        self,
        blocks: Sequence[Block],
        assignments: Sequence[Dict[str, int]],
        reservations: Optional[Dict[str, str]] = None,
        cuts: Optional[Dict[str, Cut]] = None,
    ):
        self.assignments = assignments
        self.state: Dict[str, BlockState] = {
            b.name: BlockState(
                block=b,
                thickness=b.thickness,
                reserved_for=(reservations or {}).get(b.name),
                sealed_cut=(cuts or {}).get(b.name),
            )
            for b in blocks
        }
        if (cuts or {}).keys() - {b.name for b in blocks}:
            raise ValueError("切单引用了不存在的料")

    def free_blocks(self) -> List[Block]:
        return [s.block for s in self.state.values() if s.is_free()]

    def blocks_reserved_for(self, box_name: str) -> List[Block]:
        return [
            s.block for s in self.state.values() if s.reserved_for == box_name
        ]

    def _late_box(self, late: LateOrderSpec, relation: str) -> LoanBox:
        return LoanBox(
            name=late.box_name,
            colors=tuple(late.colors),
            required_relation=relation,
            source=late.source,
            pattern=late.pattern,
        )


class PreSealPolicy(_PolicyBase):
    """策略一：首刀前封料 / 先切先得。

    封给晚盒的料早盒完全不可见。晚盒到达时：
      * 料已胶封 -> 只验证现有截面是否满足关系，不符只能拒单；
      * 料未切 -> 可按晚单实际关系选面切一次（仍受夹具模式限制）。
    """

    def serve(
        self, early_boxes: Sequence[LoanBox], late: LateOrderSpec
    ) -> Dict[LateOutcome, PolicyVerdict]:
        free = self.free_blocks()
        early_result = allocate(early_boxes, free, self.assignments)
        early_used = {
            cut.block for f in early_result.fills for cut in f.cuts
        }
        # 早盒切单已锁定：其料对晚盒不可见，晚盒只能用“封给它的料 +
        # 早盒用剩的自由料”，且不得让早盒重排。
        leftover_free = [b for b in free if b.name not in early_used]
        reserved = self.blocks_reserved_for(late.box_name)
        late_pool = reserved + leftover_free
        sealed_cuts = {
            name: st.sealed_cut
            for name, st in self.state.items()
            if st.sealed_cut is not None and name in {b.name for b in late_pool}
        }
        early_names = [f.box.name for f in early_result.fills]

        verdicts = {
            LateOutcome.NOT_ARRIVED: PolicyVerdict(
                LateOutcome.NOT_ARRIVED,
                early_names,
                False,
                "晚盒未出现：封料继续硬占用，早盒按自由料最大配发",
            )
        }

        for relation in late.possible_relations:
            late_box = self._late_box(late, relation)
            # 晚盒单独在“封给它的料 + 早盒用剩的自由料”里配发。
            result = allocate(
                [late_box],
                late_pool,
                self.assignments,
                existing_cuts=sealed_cuts,
            )
            got_late = result.box_count == 1
            sealed_names = [
                b.name for b in reserved if self.state[b.name].sealed_cut
            ]
            if got_late:
                if sealed_names:
                    reason = (
                        f"封料 {sealed_names} 已切截面关系为{relation}，"
                        "连同现有截面转出，不重锯"
                    )
                else:
                    reason = "封料未切，按晚单实际关系选面切一次"
            else:
                reason = (
                    f"封料 {sealed_names or [b.name for b in reserved]} "
                    f"已胶封且现有截面给不出{relation}，不能重锯，拒单"
                )
            verdicts[LateOutcome(relation)] = PolicyVerdict(
                LateOutcome(relation),
                early_names + ([late.box_name] if got_late else []),
                not got_late,
                reason,
            )
        return verdicts


class JigTestGrindPolicy(_PolicyBase):
    """策略二：保留同纹原块 + 双台定位架试纹。

    磨面是真实工单损耗：每磨一面扣 ``allowed_loss`` 层厚度并把该面移出
    可下刀面；定向后本体只够再切一次。厚度不覆盖“损耗 + 一次正式切”
    时 :meth:`can_test_grind` 直接为假，策略不允许假装读到了纹。
    """

    def can_test_grind(self, block_name: str, spec: LateOrderSpec) -> bool:
        st = self.state[block_name]
        if st.block.in_transit or st.sealed_cut is not None:
            return False
        if not st.remaining_modes():
            return False
        return st.thickness >= spec.allowed_loss + 1

    def test_grind(
        self, block_name: str, face: FaceMode, spec: LateOrderSpec
    ) -> BlockState:
        if not self.can_test_grind(block_name, spec):
            raise ValueError("厚度或可用面不足，不能试纹磨面")
        st = self.state[block_name]
        st.thickness -= spec.allowed_loss
        if face not in st.test_ground_faces:
            st.test_ground_faces = (*st.test_ground_faces, face)
        return st

    def adaptive_serve(
        self,
        early_boxes: Sequence[LoanBox],
        held_for_late: Sequence[str],
        late: LateOrderSpec,
    ) -> Dict[LateOutcome, PolicyVerdict]:
        """``held_for_late`` 中的料保留到晚单通知，不进早盒切单。"""
        held = set(held_for_late)
        free = [b for b in self.free_blocks() if b.name not in held]
        early_result = allocate(early_boxes, free, self.assignments)
        early_used = {
            cut.block for f in early_result.fills for cut in f.cuts
        }
        leftover = [b for b in free if b.name not in early_used]
        held_blocks = [self.state[name].block for name in held_for_late]

        verdicts = {
            LateOutcome.NOT_ARRIVED: PolicyVerdict(
                LateOutcome.NOT_ARRIVED,
                [f.box.name for f in early_result.fills],
                False,
                "晚盒未出现：保留料未磨未切，试纹能力原样保留",
            )
        }

        for relation in late.possible_relations:
            late_box = self._late_box(late, relation)

            # 试磨只在副本状态上推演：平行/正交是互斥的两个可能晚单，
            # 真实工单只会走其中一个，不能让两次评估的损耗互相累积。
            sim_states = {
                name: BlockState(
                    block=st.block,
                    thickness=st.thickness,
                    test_ground_faces=st.test_ground_faces,
                    sealed_cut=st.sealed_cut,
                    reserved_for=st.reserved_for,
                )
                for name, st in self.state.items()
            }
            grind_notes: List[str] = []
            grind_ok = True
            # 磨面后的虚拟料块：夹具从 EITHER 收缩到仅剩的那一面。
            effective_blocks: Dict[str, Block] = {
                b.name: b for b in held_blocks + leftover
            }

            for name in held_for_late:
                st = sim_states[name]
                can_grind = (
                    not st.block.in_transit
                    and st.sealed_cut is None
                    and bool(st.remaining_modes())
                    and st.thickness >= late.allowed_loss + 1
                )
                if not can_grind:
                    grind_ok = False
                    grind_notes.append(
                        f"{name} 厚度{st.thickness}/许可损耗{late.allowed_loss}"
                        " 不足或已封切"
                    )
                    continue

                # 先在留给晚盒的料内部找满足晚单关系的稳健面组合，
                # 不把早盒切单里的料拉进来。
                partner = next(
                    (
                        effective_blocks[other]
                        for other in held_for_late
                        if other != name and other in effective_blocks
                    ),
                    None,
                )
                if partner is None:
                    grind_ok = False
                    grind_notes.append(f"{name} 在晚盒保留料内没有搭档")
                    continue
                pairs = robust_face_pairs(st.block, partner, self.assignments)
                fit = [p for p in pairs if p[2] == relation]
                if not fit:
                    grind_ok = False
                    grind_notes.append(f"{name} 无{relation}稳健面")
                    continue

                # 试磨与目标面相反的那一面，把目标面留给唯一一次正式切。
                target_face = fit[0][0]
                ground_face = (
                    FaceMode.ADJACENT_ONLY
                    if target_face is FaceMode.SLOT_ONLY
                    else FaceMode.SLOT_ONLY
                )
                if ground_face not in st.remaining_modes():
                    grind_ok = False
                    grind_notes.append(f"{name} 待磨面已不可用")
                    continue
                st.thickness -= late.allowed_loss
                st.test_ground_faces = (*st.test_ground_faces, ground_face)
                effective_blocks[name] = Block(
                    name=st.block.name,
                    color=st.block.color,
                    pattern=st.block.pattern,
                    source=st.block.source,
                    face_mode=target_face,   # 磨废另一面后只能沿目标面切
                    thickness=st.thickness,
                )
                grind_notes.append(
                    f"磨 {name} 的{ground_face.value}面 -{late.allowed_loss} 层，"
                    f"余厚 {st.thickness}，本体仅够沿{target_face.value}再切一次定{relation}"
                )

            pool = list(effective_blocks.values())
            # 晚盒单独评估：早盒切单已锁定，不参与重排。
            result = allocate([late_box], pool, self.assignments)
            got_late = result.box_count == 1
            served = grind_ok and got_late
            if got_late and not grind_ok:
                grind_notes.append("关系虽可配，但试纹损耗/可用面不满足工单")
            verdicts[LateOutcome(relation)] = PolicyVerdict(
                LateOutcome(relation),
                [f.box.name for f in early_result.fills]
                + ([late.box_name] if served else []),
                not served,
                "；".join(grind_notes)
                if served
                else ("；".join(grind_notes) or "无料可在许可损耗内完成试纹定向，拒单"),
            )
        return verdicts
