"""端到端演示：设备会计 —— 购建底单、原值封存、折旧换件与独占拨付。

运行：

    python -m demo.equipment_scenarios

在 demo_data/ 下建立自己的 SQLite 库（不打扰 demo.scenarios 的产物），
依次演示题面故事线：

1. 购建底单与封存：电机 8、机座 4、安装费 3 按工时 1:2 分摊 → 原值 9 和 6；
2. 旧库部分实付后取消余款：新电机承诺 10 实付 7 取消 3，核原值仍可见；
3. 限额 6 换电机：账面 6 − 残件 0 = 损失 6 恰好可办；回单丢失按原号查；
4. 更正工时为 2:1 重封同一组实付：电机变 10、账面 7，原工单被拒；
5. 另一项已办退役占去限额 1：电机账面 6 限额剩 5，同样须拒，机座照常计龄；
6. 独占拨付终例：在途 5 挤占则 B 没足 2；一柜独占 6 按 4 及 2 先后办完；
   残件记录改到实际损失超 6，谁都不能先动旧件；
7. 迁编不改写早期入账归属；独立重算审计全部通过。

规模：12 个部件、三个期间、两台本地柜。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from orgbudget import BudgetService, OrgService
from orgbudget.equipment_service import EquipmentService, VoucherLine
from orgbudget.equipment_audit import audit_equipment
from orgbudget.errors import (
    InsufficientLossAllowance,
    OrderBlocked,
    PartNotOnMachine,
    SealStale,
)
from orgbudget.local_cabinet import LocalCabinet

DATA_DIR = Path(__file__).resolve().parent.parent / "demo_data" / "equipment"


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def main() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    org = OrgService(DATA_DIR / "org.db")
    budget = BudgetService(DATA_DIR / "budget.db", org)
    equip = EquipmentService(DATA_DIR / "equipment.db", budget)

    # 两个工坊叶：W1 主线，W2 两个“须拒”分支。
    for dept, parent in [("HQ", None), ("W1", "HQ"), ("W2", "HQ")]:
        org.create_department(dept, parent)
    v1 = org.publish("两个工坊叶").version
    product = org.get_product(v1)
    weights = {(d, k): 1 for d in product.department_ids for k in product.children(d)}
    budget.register_root_budget(v1, 100_000, weights)

    def pay(req: str, dept: str, commit: int, spend: int, idem: str,
            cancel: bool = False) -> None:
        budget.create_commitment(req, dept, commit)
        budget.register_spend(req, spend, idem)
        if cancel:
            budget.cancel_commitment(req)

    # ------------------------------------------------------------ 1. 底单与封存
    _line("案例 1：购建底单 —— 电机 8、机座 4、安装费 3（工时 1:2）")
    pay("REQ-MOTOR", "W1", 8, 8, "SP-MOTOR")
    pay("REQ-BASE", "W1", 4, 4, "SP-BASE")
    pay("REQ-INST", "W1", 3, 3, "SP-INST")
    equip.register_equipment("M1", "W1", 1, {"MOTOR": 3, "BASE": 6})
    equip.post_voucher("V-MOTOR", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)])
    equip.post_voucher("V-BASE", "M1", "SP-BASE", [VoucherLine.direct("BASE", 4)])
    equip.post_voucher(
        "V-INST", "M1", "SP-INST",
        [VoucherLine.install(3, {"MOTOR": 1, "BASE": 2})],
    )
    values = equip.seal_original_value("M1")
    print(f"  封存原值: {values}（会计封存无需等待工坊提出换件）")
    _check("安装费 3 按工时 1:2 摊 → 电机 9、机座 6", values == {"MOTOR": 9, "BASE": 6})
    trail = equip.part_money_trail("M1", "MOTOR")
    for t in trail:
        print(f"  电机分钱: 底单 {t.voucher_id} ← 实付 {t.spend_idem_key}"
              f"（承诺 {t.request_no}，叶 {t.dept_id}，组织 v{t.org_version}）"
              f" {t.kind} {t.amount} 分")
    _check("每分钱可追查回实付事件、承诺与归属叶",
           [(t.kind, t.amount) for t in trail] == [("DIRECT", 8), ("INSTALL", 1)])

    # ------------------------------------------------------------ 2. 部分实付
    _line("案例 2：旧库部分实付后取消余款 —— 承诺 10、实付 7、取消 3")
    pay("REQ-NEW", "W1", 10, 7, "SP-NEW", cancel=True)
    c = budget.get_commitment("REQ-NEW")
    print(f"  REQ-NEW 状态 {c.status}：已支 {c.spent} 分保留，余款 {c.remaining} 分释放")
    equip.post_voucher("V-NEW", "M1", "SP-NEW", [VoucherLine.direct("MOTOR2", 7)])
    values = equip.seal_original_value("M1")
    trail = equip.part_money_trail("M1", "MOTOR2")
    _check("取消余款后核原值仍看得到实付 7 分",
           values["MOTOR2"] == 7
           and [(t.spend_idem_key, t.amount) for t in trail] == [("SP-NEW", 7)])
    print(f"  重封原值: {values}；新电机 MOTOR2 在库备件，原值 7")

    # ------------------------------------------------------------ 3. 限额换件
    _line("案例 3：第 2 期期初换电机 —— 账面 6 − 残件 0 = 损失 6，限额 6 可办")
    equip.advance_period()
    print(f"  电机三期归零已过一期：账面 {equip.book_value('M1', 'MOTOR')} 分；"
          f"机座账面 {equip.book_value('M1', 'BASE')} 分")
    equip.set_loss_limit("W1", 2, 6)
    equip.disburse_to_cabinet("D1", "W1", ["M1"], "CAB-1", 6)
    cab1 = LocalCabinet(DATA_DIR / "cab1.db", "CAB-1")
    cab1.load(equip.export_disbursement("D1"))
    cab1.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
    (settlement,) = cab1.settle_all()
    print(f"  柜上核算：旧电机账面 {settlement.retired[0].book_value} − 残件 "
          f"{settlement.retired[0].salvage} = 损失 {settlement.loss}，"
          f"扣额后柜上留存 {settlement.remaining_after}")
    manifest = cab1.hand_back()
    (tx_id,) = equip.import_return(manifest)
    allowance = equip.loss_allowance("W1", 2)
    _check("损失 6 ≤ 限额 6，一次拆下旧件装上新件",
           settlement.loss == 6 and manifest["remainder"] == 0
           and allowance.confirmed == 6)
    motor2 = equip.part_status("M1", "MOTOR2")
    _check("新件单独计龄（第 2 期起、三期归零），旧电机退役",
           (motor2.state, motor2.install_period, motor2.zero_periods)
           == ("INSTALLED", 2, 3)
           and equip.part_status("M1", "MOTOR").state == "RETIRED")

    info = equip.lookup_installation("M1", "MOTOR")
    print(f"  回单丢失，按退役件原号 MOTOR 查到交易 {info.tx_id}："
          f"装上 {[p.part_no for p in info.installed]}")
    _check("用退役件原号查到此次装了什么",
           [p.part_no for p in info.installed] == ["MOTOR2"])
    equip.set_loss_limit("W1", 2, 7)
    equip.disburse_to_cabinet("D1B", "W1", ["M1"], "CAB-1", 1)
    cab1.load(equip.export_disbursement("D1B"))
    cab1.register_order("O1B", "M1", {"MOTOR": 0}, {})
    try:
        cab1.settle_all()
        raise AssertionError("应当拒绝")
    except PartNotOnMachine as exc:
        print(f"  按猜重拆被拒: {exc}")
    _check("已拆旧件不可能按猜重拆", True)
    equip.import_return(cab1.hand_back())

    # ------------------------------------------------------------ 4. 重封拒单
    _line("案例 4：更正工时为 2:1 重封同一组实付 —— 电机变 10、账面 7，原工单被拒")
    equip.register_equipment("M3", "W2", 1, {"MOTOR": 3, "BASE": 6})
    for req, sp, part, amount in [
        ("REQ3-MOTOR", "SP3-MOTOR", "MOTOR", 8),
        ("REQ3-BASE", "SP3-BASE", "BASE", 4),
    ]:
        pay(req, "W2", amount, amount, sp)
        equip.post_voucher(f"V3-{part}", "M3", sp, [VoucherLine.direct(part, amount)])
    pay("REQ3-INST", "W2", 3, 3, "SP3-INST")
    equip.post_voucher("V3-INST", "M3", "SP3-INST",
                       [VoucherLine.install(3, {"MOTOR": 1, "BASE": 2})])
    print(f"  M3 初封: {equip.seal_original_value('M3')}")
    equip.correct_install_hours("V3-INST", 1, {"MOTOR": 2, "BASE": 1})
    try:
        equip.original_values("M3")
        raise AssertionError("应当报封存过期")
    except SealStale as exc:
        print(f"  底单变动后核原值被拒: {exc}")
    values = equip.seal_original_value("M3")
    book = equip.book_value("M3", "MOTOR")
    print(f"  重封: {values}；电机已过一期，账面 {book}")
    _check("电机变 10、账面 7", values["MOTOR"] == 10 and book == 7)
    equip.set_loss_limit("W2", 2, 6)
    try:
        equip.disburse_to_cabinet("D3", "W2", ["M3"], "CAB-1", 7)
        raise AssertionError("应当拒绝")
    except InsufficientLossAllowance as exc:
        print(f"  中心拨不出 7: {exc}")
    equip.disburse_to_cabinet("D3", "W2", ["M3"], "CAB-1", 6)
    cab1.load(equip.export_disbursement("D3"))
    cab1.register_order("O3", "M3", {"MOTOR": 0}, {})
    try:
        cab1.settle_all()
        raise AssertionError("应当拒绝")
    except InsufficientLossAllowance as exc:
        print(f"  柜上核算损失 7 超留存 6，原工单被拒: {exc}")
    _check("重封后账面 7 超限额 6，工单办不成", True)
    equip.import_return(cab1.hand_back())  # 未动旧件，余款 6 交还

    # ------------------------------------------------------------ 5. 限额被占
    _line("案例 5：另一项已办退役占去限额 1 —— 电机账面 6 限额剩 5，同样须拒")
    equip.register_equipment("M4", "W2", 1, {"MOTOR": 3, "BASE": 6})
    equip.register_equipment("M5", "W2", 1, {"X": 2})
    for req, sp, part, amount in [
        ("REQ4-MOTOR", "SP4-MOTOR", "MOTOR", 8),
        ("REQ4-BASE", "SP4-BASE", "BASE", 4),
    ]:
        pay(req, "W2", amount, amount, sp)
        equip.post_voucher(f"V4-{part}", "M4", sp, [VoucherLine.direct(part, amount)])
    pay("REQ4-INST", "W2", 3, 3, "SP4-INST")
    equip.post_voucher("V4-INST", "M4", "SP4-INST",
                       [VoucherLine.install(3, {"MOTOR": 1, "BASE": 2})])
    pay("REQ5-X", "W2", 2, 2, "SP5-X")
    equip.post_voucher("V5-X", "M5", "SP5-X", [VoucherLine.direct("X", 2)])
    equip.seal_original_value("M4")
    equip.seal_original_value("M5")
    equip.disburse_to_cabinet("D5", "W2", ["M5"], "CAB-1", 1)
    cab1.load(equip.export_disbursement("D5"))
    cab1.register_order("O5", "M5", {"X": 0}, {})
    cab1.settle_all()
    equip.import_return(cab1.hand_back())
    allowance = equip.loss_allowance("W2", 2)
    print(f"  M5 退役损失 1 已确认：限额 6 剩 {allowance.available}")
    try:
        equip.disburse_to_cabinet("D4", "W2", ["M4"], "CAB-1", 6)
        raise AssertionError("应当拒绝")
    except InsufficientLossAllowance as exc:
        print(f"  M4 电机账面 {equip.book_value('M4', 'MOTOR')}，拨付被拒: {exc}")
    _check("限额被占 1 后换电机须拒", allowance.available == 5)
    _check("机座照常计龄（原值 6 已过 1 期 → 账面 5）",
           equip.book_value("M4", "BASE") == 5)

    # ------------------------------------------------------------ 6. 独占拨付终例
    _line("案例 6：余额 6、两台设备两张拆单 —— 在途挤占 / 一柜先后办 / 超限都别动")
    equip.advance_period()  # 第 3 期
    equip.register_equipment("MA", "W1", 2, {"PA": 2})
    equip.register_equipment("MB", "W1", 2, {"PB": 2})
    for req, sp, eq_id, part, amount in [
        ("REQ-PA", "SP-PA", "MA", "PA", 14),
        ("REQ-PA2", "SP-PA2", "MA", "PA2", 5),
        ("REQ-PB", "SP-PB", "MB", "PB", 4),
        ("REQ-PB2", "SP-PB2", "MB", "PB2", 3),
    ]:
        pay(req, "W1", amount, amount, sp)
        equip.post_voucher(f"V-{part}", eq_id, sp, [VoucherLine.direct(part, amount)])
    equip.seal_original_value("MA")
    equip.seal_original_value("MB")
    equip.set_loss_limit("W1", 3, 6)
    print(f"  A 旧件账面 {equip.book_value('MA', 'PA')}（预估损失 5，残件待验），"
          f"B 旧件账面 {equip.book_value('MB', 'PB')}（固定要 2）")

    print("  —— 形态一：A 占 5 的拨付在途、中心没收到余款回执")
    equip.disburse_to_cabinet("D-A", "W1", ["MA"], "CAB-1", 5)
    try:
        equip.disburse_to_cabinet("D-B", "W1", ["MB"], "CAB-2", 2)
        raise AssertionError("应当拒绝")
    except InsufficientLossAllowance as exc:
        print(f"  B 此时没足 2: {exc}")
    cab1.load(equip.export_disbursement("D-A"))
    equip.import_return(cab1.hand_back())  # 未使用，余款 5 交还
    _check("在途 5 挤占限额，交还后才可再用",
           equip.loss_allowance("W1", 3).available == 6)

    print("  —— 形态二：残件记录使 A 实际损失超 6，谁都不能先动旧件")
    equip.disburse_to_cabinet("D-C", "W1", ["MA", "MB"], "CAB-1", 6)
    cab1.load(equip.export_disbursement("D-C"))
    cab1.register_order("OA", "MA", {"PA": 0}, {"PA2": 2}, estimated_loss=5)
    cab1.register_order("OB", "MB", {"PB": 0}, {"PB2": 2})
    try:
        cab1.settle_next()
        raise AssertionError("应当拒绝")
    except InsufficientLossAllowance as exc:
        print(f"  A 实际损失 7 超留存 6: {exc}")
    try:
        cab1.settle_order("OB")
        raise AssertionError("应当拒绝")
    except OrderBlocked as exc:
        print(f"  B 不能先动: {exc}")
    _check("A 超限未了，B 也不能先动旧件", True)
    equip.import_return(cab1.hand_back())  # 两单作废，余款 6 交还

    print("  —— 形态三：柜上收到独占余额 6 并看见两套实件，按 4 及 2 先后办完")
    cab2 = LocalCabinet(DATA_DIR / "cab2.db", "CAB-2")
    equip.disburse_to_cabinet("D-D", "W1", ["MA", "MB"], "CAB-2", 6)
    cab2.load(equip.export_disbursement("D-D"))
    cab2.register_order("OA", "MA", {"PA": 3}, {"PA2": 2}, estimated_loss=5)
    cab2.register_order("OB", "MB", {"PB": 0}, {"PB2": 2})
    first, second = cab2.settle_all()
    manifest = cab2.hand_back()
    print(f"  A 预估 5、柜上查实残件 3 → 损失 {first.loss}；"
          f"B 损失 {second.loss}；余款 {manifest['remainder']}")
    equip.import_return(manifest)
    allowance = equip.loss_allowance("W1", 3)
    _check("按 4 及 2 先后办完，限额恰好用尽",
           (first.loss, second.loss, allowance.confirmed, allowance.available)
           == (4, 2, 6, 0))

    # ------------------------------------------------------------ 7. 迁编与审计
    _line("案例 7：迁编不改写早期入账归属；独立重算审计")
    org.create_department("D", "HQ")
    org.move_subtree("W1", "D")
    v2 = org.publish("W1 迁到 D 下").version
    product2 = org.get_product(v2)
    weights2 = {(d, k): 1 for d in product2.department_ids
                for k in product2.children(d)}
    budget.revise_budget(v2, 100_000, weights2)
    voucher = equip.get_voucher("V-INST")
    _check("早期底单（含安装费）归属不迁新分部",
           (voucher.dept_id, voucher.org_version) == ("W1", 1))
    _check("核原值与分钱查询不随迁编改变",
           equip.original_values("M1")["MOTOR"] == 9
           and sum(t.amount for t in equip.part_money_trail("M1", "MOTOR")) == 9)
    result = audit_equipment(equip.db_path, budget.db_path)
    for item in result.checks:
        print(f"    - {item}")
    _check("独立重算审计全部通过",
           result.replacements == 4 and result.vouchers == 15
           and result.total_confirmed_loss == 6 + 1 + 6)

    _line("全部演示完成")
    print(f"  数据库目录: {DATA_DIR}")
    print("  规模：12 个部件、三个期间、两台本地柜（CAB-1 / CAB-2）")


if __name__ == "__main__":
    main()
