# 联赛后勤洗衣规划子系统 — 设计说明

在既有“赛程生产服务 + 赛季排名仓”（旧仓）之上新增一个**只读消费方**：
从旧仓已发布的 `edition` 与比分事实（played/cancelled）派生待洗布件，
判定每件/整袋可共容的洗涤程序，并在实筒、容量、用时与交还截止下给出可执行
的分袋、整筒、交回方案——或给出缺件/时窗不足/错误送洗的可定位理由。

本子系统不改写旧仓任何赛程或比分；`materials/633/case.json` 与旧仓测试均不变。

## 1. 目录

```
materials/633/
├── laundry/
│   ├── model.py   # 布件/约束/网袋/洗衣筒；约束→允许程序集合
│   ├── towash.py  # 由 edition+事实派生待洗件（played/cancelled、归属校验）
│   ├── solver.py  # 可行性搜索、两法、坏筒失效、已开机重开
│   ├── oracle.py  # 独立枚举 oracle，用于双向对照（≤12件/6筒小域）
│   ├── service.py # 对接旧仓的端到端入口 WashRequest / plan_request
│   └── __init__.py
├── demo_laundry.py            # 题面 A 场景与旧仓联动演示
└── tests/test_laundry.py      # 洗衣子系统测试（与 oracle 对照）
```

```bash
python3 demo_laundry.py
python3 -m unittest discover -s tests
```

## 2. 数据模型与“约束→允许程序集合”

- **具名布件** `ItemInfo(item_id, team, side, kind, size)`：主/客场单独登记，
  从不按“同尺码”跨队顶替（`towash` 显式校验领用件属于该场该方，否则驳回）。
- **约束** `Constraint(source, allowed)`：磨耗、历次修补材料、内衬、本场用法各给
  一个允许程序集合。一件布件最终 `allowed = ⋂ 各约束集合`；交集为空即
  **不可洗**——`plan` 直接判不可行并给 `no_allowed_program` 理由，即使没有
  下一场也能据此**驳回错误送洗**。
- **网袋** `Bag`：`open` 可任意拆/合；`sealed`（有回条）后同回条不得在更衣室拆开；
  `washing/done` 后不可在机里拆。**网袋在洗完之前不能在机里拆**。
- **洗衣筒** `Drum(program, start, capacity, duration, machine_id)`：整点启动、
  单程序、单容量；`end=start+duration`。同机两筒时间重叠即不可同案执行。

## 3. 待洗派生（`towash.derive_to_wash`）

按**实际发布 edition** 取 `home/away`，只把 `state == played` 场次中原在主/客方
领用、赛后仍在运动员手里的布件纳入待洗，并叠加本场 `usage`。`cancelled` 只退回
未用件、`pending` 亦不产生待洗。因此**同一比赛改报取消**（旧仓以新 request 批
替换该 match 的 active result）会从待洗集合移除该场本方布件——tests 与 demo 均
以真实 SQLite 复现这一变化。

同一布件可出现在多场 played 中，约束逐场累积（账内磨耗/材料/内衬常驻，用法逐场）。

## 4. 求解（`solver`）

- **指派单元（束）**：已开机筒内件冻结、每件固定；同一已封回条袋内件必须同筒
  （否则判 `sealed_bag_no_common_program` / `sealed_bag_split`）；其余每件独立，
  开放袋可自由拆合。回溯分配“束→筒”，尊重程序允许、容量、截止，并强制
  **各件只准在一处**。
- **目标袋/操作**：由 item→drum 指派反推目标袋与 `keep/split/merge/new` 操作，
  满足“封车前照案拆合、封袋”。
- **理由**：不可行时给 `no_drum_before_deadline` / `no_drum_for_program` /
  `insufficient_capacity` / `total_capacity_short` / `set_incomplete` 等可定位码。

## 5. 两法（互斥且共享同一实筒单）

- **方案先行 `scheme_first`**：装备员一次给出多个互斥合法袋案；车场按全部实筒
  挑定能齐套的一案。某筒封车前坏掉时，`invalidated_schemes` 返回**用到该筒即
  失效**的整套袋案（可证明：其余案不含该筒，仍可执行）。
- **实筒先行 `drum_first`**：车场报实筒与时刻，装备员逐筒确认可共存分袋后封袋。
  由于指派对每件至多一处，跨筒承诺在两两间不在一件布上相撞；同机时间重叠被
  剔除。两法掌握**相同的实筒单**，封车前都可改袋。

## 6. 已开机后重开（`replan`）

某筒已开机、回报未到更衣室时，该筒内件 `frozen`：不重洗、不再许给别筒/别队；
其余件在现存实筒上重新求解，保证全局每件至多一处。若冻结筒完成晚于截止，给
`started_drum_late` 并相应套判缺件。

## 7. 与旧仓的一致性

`service.derive_for_request` 用 `ScheduleService.get_edition` 取真实主客，
用 `StandingsRepository.active_results` 取当前事实切面。旧仓的 edition 不可变、
快照不复写等性质原样沿用；洗衣子系统只是这些事实的一个纯函数消费方。
