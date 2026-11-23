"""采果份额账本：按实付与有效未付担保发行整串份额。

规则（对应题面）：

* 每张认养原件记承诺号、叶部门、树行、品种、级别；每 **5 分**预付买一整串；
  合同允许时，每 5 分有效未付担保可先锁一串。
* 实付部分与担保部分**各自向下取整，零头不合并**：``floor(paid/5)`` 与
  ``floor(guarantee/5)`` 分别成份额，任何跨承诺、跨类别的零头都不能凑成一串
  （即“承诺之间不能互相填零”）。
* 办事处可按双方同意，把未用份额让给预算所采用的同一分部另一叶；原合同的
  树行、品种、级别随份额走。
* 取消承诺时，实付仍购买果串；未剪的担保份额不再可用。已剪走的份额不得再转。

本模块只做份额账本，不读取果树几何；与 :class:`orgbudget.BudgetService`
的衔接通过 :class:`CommitmentSnapshot` 完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .errors import (
    IneligibleTransfer,
    ShareAlreadyConsumed,
    UnknownCommitment,
    UnknownShare,
    VerificationError,
)

PAID = "PAID"
GUARANTEE = "GUARANTEE"
_KINDS = (PAID, GUARANTEE)
UNIT_FEN = 5


@dataclass(frozen=True)
class CommitmentSnapshot:
    """一条预算承诺在发行份额时的事实快照。

    ``paid`` = 已实际支出（实付）；``guarantee`` = 有效未付担保剩余。
    与 :class:`orgbudget.Commitment` 对应：``paid=spent``、``guarantee=remaining``。
    """

    request_no: str
    dept_id: str
    paid: int
    guarantee: int
    cancelled: bool = False

    @classmethod
    def from_commitment(cls, commitment) -> "CommitmentSnapshot":
        """从 orgbudget 的 ``Commitment`` 取快照；取消后担保不再有效。"""
        cancelled = commitment.status == "CANCELLED"
        return cls(
            request_no=commitment.request_no,
            dept_id=commitment.dept_id,
            paid=commitment.spent,
            guarantee=0 if cancelled else commitment.remaining,
            cancelled=cancelled,
        )


@dataclass(frozen=True)
class Adoption:
    """认养原件：承诺号 + 叶部门 + 树行/品种/级别 + 是否允许担保锁串。"""

    contract_no: str
    commitment_no: str
    dept_id: str
    row: str
    variety: str
    grade: str
    guarantee_allowed: bool = True


@dataclass
class Share:
    """一个可换一整串果的 5 分份额。"""

    share_id: str
    contract_no: str
    commitment_no: str
    origin_dept: str
    dept_id: str          # 当前归属叶（转让后会变化）
    row: str
    variety: str
    grade: str
    kind: str             # PAID / GUARANTEE
    consumed_string: Optional[str] = None
    revoked: bool = False

    @property
    def is_available(self) -> bool:
        return self.consumed_string is None and not self.revoked

    @property
    def is_consumed(self) -> bool:
        return self.consumed_string is not None


class ShareLedger:
    """按承诺发行 / 转让 / 使用份额的账本。"""

    def __init__(self) -> None:
        self._shares: dict[str, Share] = {}
        self._by_commitment: dict[str, list[str]] = {}
        self._issued_counts: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- 发行

    def adopt(self, adoption: Adoption, snapshot: CommitmentSnapshot) -> tuple[Share, ...]:
        """为一张认养原件按承诺实付/担保发行份额（两部分各自下取整）。

        幂等：重复调用只为新增的整 5 分补发，绝不重复发行。已取消的承诺
        不再新发担保份额（已有担保份额由 :meth:`revoke_guarantees` 作废）。
        """
        if snapshot.request_no != adoption.commitment_no:
            raise UnknownCommitment(
                f"认养原件承诺号 {adoption.commitment_no} 与快照 {snapshot.request_no} 不一致"
            )
        if snapshot.dept_id != adoption.dept_id:
            raise UnknownCommitment(
                f"承诺 {snapshot.request_no} 属于叶 {snapshot.dept_id}，"
                f"认养原件却记 {adoption.dept_id}"
            )
        paid_n = snapshot.paid // UNIT_FEN
        guarantee_n = 0
        if adoption.guarantee_allowed and not snapshot.cancelled:
            guarantee_n = max(0, snapshot.guarantee) // UNIT_FEN
        issued: list[Share] = []
        issued.extend(self._ensure(adoption, snapshot, PAID, paid_n))
        issued.extend(self._ensure(adoption, snapshot, GUARANTEE, guarantee_n))
        return tuple(issued)

    def _ensure(
        self, adoption: Adoption, snapshot: CommitmentSnapshot, kind: str, target: int
    ) -> list[Share]:
        """把该承诺的 ``kind`` 份额补齐到 ``target``，只发差额。"""
        have = self._issued_counts.get((adoption.commitment_no, kind), 0)
        return self._issue(adoption, snapshot, kind, max(0, target - have))

    def _issue(
        self, adoption: Adoption, snapshot: CommitmentSnapshot, kind: str, count: int
    ) -> list[Share]:
        out: list[Share] = []
        for _ in range(count):
            n = self._issued_counts.get((adoption.commitment_no, kind), 0)
            share_id = f"{adoption.commitment_no}:{kind}:{n}"
            self._issued_counts[(adoption.commitment_no, kind)] = n + 1
            share = Share(
                share_id=share_id,
                contract_no=adoption.contract_no,
                commitment_no=adoption.commitment_no,
                origin_dept=adoption.dept_id,
                dept_id=adoption.dept_id,
                row=adoption.row,
                variety=adoption.variety,
                grade=adoption.grade,
                kind=kind,
            )
            self._shares[share_id] = share
            self._by_commitment.setdefault(adoption.commitment_no, []).append(share_id)
            out.append(share)
        return out

    def reissue_after_spend(
        self, adoption: Adoption, snapshot: CommitmentSnapshot
    ) -> tuple[Share, ...]:
        """承诺又发生实付后，仅为新增的整 5 分实付补发份额。

        担保份额不因后续支出重复发行；已发行的数量由账本记忆，
        保证多次调用只补差额。
        """
        if snapshot.request_no != adoption.commitment_no:
            raise UnknownCommitment("承诺号不一致")
        want_paid = snapshot.paid // UNIT_FEN
        have_paid = self._issued_counts.get((adoption.commitment_no, PAID), 0)
        new_paid = max(0, want_paid - have_paid)
        return tuple(self._issue(adoption, snapshot, PAID, new_paid))

    # ---------------------------------------------------------------- 查询

    def share(self, share_id: str) -> Share:
        try:
            return self._shares[share_id]
        except KeyError:
            raise UnknownShare(f"份额 {share_id} 不存在") from None

    def shares_of(self, commitment_no: str) -> tuple[Share, ...]:
        return tuple(self._shares[s] for s in self._by_commitment.get(commitment_no, ()))

    def available_shares(
        self, dept_id: Optional[str] = None, *, kind: Optional[str] = None
    ) -> tuple[Share, ...]:
        return tuple(
            s
            for s in self._shares.values()
            if s.is_available
            and (dept_id is None or s.dept_id == dept_id)
            and (kind is None or s.kind == kind)
        )

    # ---------------------------------------------------------------- 转让

    def transfer(
        self,
        share_id: str,
        to_dept: str,
        *,
        product,
    ) -> Share:
        """把未用份额让给同一分部另一叶；树行/品种/级别随份额走。

        ``product`` 提供 ``contains`` / ``is_leaf`` / ``is_active`` /
        ``ancestors``，通常为 :class:`orgbudget.OrgProduct`。
        """
        share = self.share(share_id)
        if share.is_consumed:
            raise ShareAlreadyConsumed(f"份额 {share_id} 已剪走，不得再转")
        if share.revoked:
            raise IneligibleTransfer(f"份额 {share_id} 已作废，不能转让")
        if to_dept == share.dept_id:
            raise IneligibleTransfer(f"份额 {share_id} 已在叶 {to_dept}，无需转让")
        if not product.contains(to_dept):
            raise IneligibleTransfer(f"目标叶 {to_dept} 不在预算采用的组织产品中")
        if not product.is_leaf(to_dept):
            raise IneligibleTransfer(f"目标 {to_dept} 不是叶部门")
        if not product.is_active(to_dept):
            raise IneligibleTransfer(f"目标叶 {to_dept} 已停用")
        if _division_of(product, share.dept_id) != _division_of(product, to_dept):
            raise IneligibleTransfer(
                f"份额 {share_id} 跨分部：{share.dept_id} 与 {to_dept} 不同分部，"
                "不允许转让"
            )
        # 原地新增一条记录，保留原归属可追溯。
        moved = Share(
            share_id=share.share_id,
            contract_no=share.contract_no,
            commitment_no=share.commitment_no,
            origin_dept=share.origin_dept,
            dept_id=to_dept,
            row=share.row,
            variety=share.variety,
            grade=share.grade,
            kind=share.kind,
        )
        self._shares[share_id] = moved
        return moved

    # ---------------------------------------------------------------- 使用与作废

    def consume(self, share_id: str, string_id: str) -> Share:
        """用某份额交付一整串；一串一份额，不可复用。"""
        share = self.share(share_id)
        if share.revoked:
            raise ShareAlreadyConsumed(f"份额 {share_id} 已作废，不能使用")
        if share.is_consumed:
            raise ShareAlreadyConsumed(
                f"份额 {share_id} 已用于果串 {share.consumed_string}"
            )
        if any(s.consumed_string == string_id for s in self._shares.values()):
            raise ShareAlreadyConsumed(f"果串 {string_id} 已被另一份额交付")
        share.consumed_string = string_id
        return share

    def revoke_guarantees(self, commitment_no: str) -> tuple[str, ...]:
        """承诺取消：未剪的担保份额作废；已剪的、实付份额保持有效。"""
        revoked: list[str] = []
        for sid in self._by_commitment.get(commitment_no, ()):
            share = self._shares[sid]
            if share.kind == GUARANTEE and share.is_available:
                share.revoked = True
                revoked.append(sid)
        return tuple(revoked)

    # ---------------------------------------------------------------- 核对

    def verify_claim(
        self,
        claim: "Claim",
        *,
        string_attrs: Optional[Mapping[str, tuple[str, str, str]]] = None,
    ) -> "ClaimVerification":
        """独立核对一整份逐户领果要求。

        每一串必须能追溯到**唯一一笔**实付或担保（一条份额一份额来源），
        列明来源；任何“两笔承诺凑一串”的写法都被判为互相填零而拒绝。
        同一份额或同一果串在一份领果要求里不得重复占用。
        """
        seen_shares: set[str] = set()
        seen_strings: set[str] = set()
        provenances: list[Provenance] = []
        for line in claim.lines:
            if line.string_id in seen_strings:
                raise VerificationError(f"果串 {line.string_id} 在领果要求中重复出现")
            seen_strings.add(line.string_id)
            share = self._resolve_line(line)
            if share.share_id in seen_shares:
                raise VerificationError(
                    f"份额 {share.share_id} 被领果要求重复使用"
                )
            seen_shares.add(share.share_id)
            if share.revoked:
                raise VerificationError(
                    f"份额 {share.share_id} 已作废（担保在取消后未剪即失效）"
                )
            if share.is_consumed and share.consumed_string != line.string_id:
                raise VerificationError(
                    f"份额 {share.share_id} 已交付果串 {share.consumed_string}，"
                    f"不能再交付 {line.string_id}"
                )
            if string_attrs is not None:
                want = (share.row, share.variety, share.grade)
                got = string_attrs.get(line.string_id)
                if got is None:
                    raise VerificationError(f"果串 {line.string_id} 不在挂牌清单中")
                if tuple(got) != want:
                    raise VerificationError(
                        f"果串 {line.string_id} 属性 {tuple(got)} 与份额 "
                        f"{share.share_id} 的 {want} 不符"
                    )
            provenances.append(
                Provenance(
                    string_id=line.string_id,
                    share_id=share.share_id,
                    commitment_no=share.commitment_no,
                    kind=share.kind,
                    dept_id=share.dept_id,
                    row=share.row,
                    variety=share.variety,
                    grade=share.grade,
                )
            )
        return ClaimVerification(
            household_id=claim.household_id,
            provenances=tuple(provenances),
            source_commitments=frozenset(p.commitment_no for p in provenances),
        )

    def _resolve_line(self, line: "ClaimLine") -> Share:
        candidates = list(line.share_ids)
        if line.fen_sources:
            commitments = {c for c, _k, _f in line.fen_sources}
            if len(commitments) > 1:
                raise VerificationError(
                    "果串 %s 试图用多笔承诺的零头凑数（互相填零）：%s"
                    % (line.string_id, sorted(commitments))
                )
            total_fen = sum(f for _c, _k, f in line.fen_sources)
            if total_fen != UNIT_FEN:
                raise VerificationError(
                    f"果串 {line.string_id} 的来源合计 {total_fen} 分，不是一整串的 "
                    f"{UNIT_FEN} 分"
                )
            commitment_no, kind, _fen = line.fen_sources[0]
            match = [
                s
                for s in self.shares_of(commitment_no)
                if s.kind == kind and s.is_available and s.share_id not in candidates
            ]
            if not match:
                raise VerificationError(
                    f"承诺 {commitment_no} 没有可用的 {kind} 份额可供果串 {line.string_id}"
                )
            candidates.append(match[0].share_id)
        if len(candidates) != 1:
            if len(candidates) > 1:
                raise VerificationError(
                    f"果串 {line.string_id} 挂了多个来源份额 {candidates}，"
                    "一串只能由一份额支撑（零头不合并）"
                )
            raise VerificationError(f"果串 {line.string_id} 没有任何来源份额")
        return self.share(candidates[0])


@dataclass(frozen=True)
class ClaimLine:
    """逐户领果要求的一行：一串果及其来源。

    正常写法用 ``share_ids``（恰一条）；也可用 ``fen_sources`` 列明
    ``(承诺号, 类别, 分)``，用于核对“不能互相填零”。
    """

    string_id: str
    share_ids: tuple[str, ...] = ()
    fen_sources: tuple[tuple[str, str, int], ...] = ()


@dataclass(frozen=True)
class Claim:
    household_id: str
    lines: tuple[ClaimLine, ...]


@dataclass(frozen=True)
class Provenance:
    string_id: str
    share_id: str
    commitment_no: str
    kind: str
    dept_id: str
    row: str
    variety: str
    grade: str


@dataclass(frozen=True)
class ClaimVerification:
    household_id: str
    provenances: tuple[Provenance, ...]
    source_commitments: frozenset[str]

    def describe(self) -> str:
        rows = [
            f"{p.string_id}<-{p.commitment_no}/{p.kind}({p.share_id})"
            for p in self.provenances
        ]
        return "; ".join(rows)


def _division_of(product, dept_id: str) -> str:
    """叶所属“分部”：根之下的一级部门；若叶直挂根，则为其自身。"""
    ancestors = product.ancestors(dept_id)
    if len(ancestors) >= 2:
        return ancestors[1]
    return dept_id


def fen_pools(ledger: ShareLedger, commitments: Mapping[str, CommitmentSnapshot]) -> dict[str, tuple[int, int]]:
    """各承诺尚未成份额的零头（实付、担保各自 mod 5），用于说明不能填零。"""
    pools: dict[str, tuple[int, int]] = {}
    for request_no, snap in commitments.items():
        paid_left = snap.paid % UNIT_FEN
        guar_left = 0 if snap.cancelled else max(0, snap.guarantee) % UNIT_FEN
        pools[request_no] = (paid_left, guar_left)
    return pools
