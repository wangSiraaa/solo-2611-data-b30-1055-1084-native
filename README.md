# 档案保全解释与整案移交（archive_handoff）

两个职责完全独立的服务，共用 SQLite 持久化，仅通过“资格产品（qualification product）”
交换数据。全部为虚构资料，不执行真实删除，也不做现实合规判断。

```
archive_handoff/
  models.py          # 不可变领域记录（Rule/Document/Event/Hold/资格产品）
  rules_service.py   # 规则/事件/保全目录 + 资格计算 + 产品的冻结与落盘
  handoff_service.py # 整案移交仓：保管责任、整案校验、准备/确认/计划、对账
  materials.py       # 只读导入 materials/751 或任意同布局目录
  scenario.py        # 文档化 751 走查、operations 回放、200/10/500/100 规模生成
  binding.py         # 纸叶装配单、支承表、可达性、补条新测签认
  photography.py     # 摄影台/夹手、单/双夹支架排程、定档框分析
  binding_service.py # 真支承单与成像单的编排（保管方视角）
  binding_scenario.py# 文档化纸叶+翻拍走查（A/B/C/D 六个用例）
  cli.py             # 命令行
tests/               # unittest（无需 pytest）
```

## 核心规则（规则/事件侧）

- 每档案有稳定 `doc_id`、`case_id`、一个或多个规则 ID。
- 每条规则给触发事件类型与非负保存天数：`计算日 = 最新适用事件日 + 保存天数`。
- 一个档案的**所有规则都满足**才谈时间条件；计算日取各规则计算日的**最大值**。
  任一必要触发缺失 → 该档案**待定**，**不把缺失事件当作第 0 天**。
- 查询日（整数）**达到**计算日即满足时间条件（`as_of >= computed`）。
- 保全可针对档案或整个 case，区间 `[start, end)`，`end` 为空表示未释放。
  查询日任一适用保全有效即禁止移交。
- **保全不暂停保存期时钟**：冻结天数不追加到到期日。D1 的计算日始终是 30，
  不是 30+25。
- 逐档案输出：计算来源（哪条规则、哪个事件、哪次修订）、有效保全、缺触发原因。
  同一档案可有**多个阻止原因**（如 `hold` + `not_due`），明细按稳定键去重，只出现一次。
- 输入批次**整批接受或整批拒绝**：同号同文复读为幂等 no-op；同号异文、
  非法区间（`end <= start`）、未知引用（未知 doc/rule/case）一律整批拒绝。
- 资格产品按固定 `as_of_day` 冻结，记录 `source_revision` 与各输入修订；
  历史产品不可被后续目录倒改。

## 移交仓（移交侧）

- 拥有**自己的**保管责任状态，初始每案卷为 `LOCAL`。责任状态 = 追加式流水
  `custody_events` + 维护的当前态 `custody_current`；打开时按流水归约对账，
  维护态被篡改会被修复。
- case 必须**整案交接**。一次请求明确 `case_id`、**完整** `doc_ids` 集合、
  `from`、`to`、单一 `as_of_day`。
  - 集合必须**恰好等于**当前案卷成员：少一个成员、或多一个，都拒绝（`not_whole_case`）。
  - 每份成员必须在该查询日的资格产品中 `releasable=true`，否则整案拒绝
    （`ineligible_members`）。
  - 当前保管人必须等于 `from`，否则拒绝（`custody_mismatch`）。
  - 单份都合格但漏成员，同样拒绝；**不能**把规则侧“可移交=true”的行直接导出就算完成。
- 移交侧**不重算**保全日期：它只消费规则侧产出的资格产品（按内容摘要校验）。
- 成功请求在**一个本地事务**内：写交接记录（固定实际成员、资格来源、交接 ID）、
  追加保管流水、更新当前保管人。
- 同 `handoff_id` 同文复读 → 只读回冻结记录；异文 → 拒绝。
- 已移交案卷再用新 ID 声称从 `LOCAL` 交出 → `custody_mismatch` 拒绝。
- **准备/计划失效**：发布计划（`prepare_and_publish`）或准备提案（`prepare`）时冻结
  成员、保管人与逐档资格摘要。确认时若成员、保管责任或相关资格任一变化，
  旧准备整批拒绝（`stale_plan` / `stale_proposal`），不会半案生效。
- **历史固定**：历史计划/提案/交接成员不按当前目录倒改。

## 命令

```bash
DATA=artifacts/demo

# 导入 materials/751（只读输入）
python3 -m archive_handoff.cli --data $DATA import-materials --materials materials/751

# 文档化 751 全流程（生成 artifacts/ 下的 P1.json / P2.json / scenario.json）
python3 -m archive_handoff.cli --data $DATA demo

# 回放 materials/751/operations.jsonl（P1 过期被拒、P2 可用、P1 历史只读）
python3 -m archive_handoff.cli --data $DATA operations

# 发布资格产品（固定查询日，落盘 products/<id>.json）
python3 -m archive_handoff.cli --data $DATA publish --day 40 --product Q40

# 修改保全（修订号递增；旧修订/异文整批拒绝）
python3 -m archive_handoff.cli --data $DATA revise-hold \
  --hold '{"hold_id":"H1","revision":2,"case_id":"C1","start":25,"end":35}'

# 单条移交请求
python3 -m archive_handoff.cli --data $DATA submit \
  --request '{"handoff_id":"WHOLE1","case_id":"C1","doc_ids":["D1","D2"],"from":"LOCAL","to":"VAULT1","as_of_day":40}'

# 从文件批量提交（每行一条完整请求；--allow-rejects 忽略个体拒绝）
python3 -m archive_handoff.cli --data $DATA requests --file my_requests.jsonl --allow-rejects

# 自定义文件导入（rules/documents/events/holds/materials）
python3 -m archive_handoff.cli --data $DATA ingest --kind holds --file extra_holds.json

# 状态与重开核对
python3 -m archive_handoff.cli --data $DATA status
python3 -m archive_handoff.cli --data $DATA verify

# 规模数据：200 档案 / 10 规则 / 500 事件+保全 / 100 请求（生成+校验+重开）
python3 -m archive_handoff.cli --data artifacts/scale scale

# 纸叶支撑 + 翻拍走查（生成 artifacts/binding_demo.json / work_order.json）
python3 -m archive_handoff.cli --data artifacts/binding binding-demo

# 整册支承见证 / 单姿态核对 / 单缝查询
python3 -m archive_handoff.cli binding-support --pose 111 --seam F1

# 在夹手支架上给一张（或多张）目标图排动作（默认单夹；--double 用双夹）
python3 -m archive_handoff.cli binding-plan --clamp 6 \
  --target '{"shot_id":"T1","seam":"F1","level":1,"must_see":[2,3]}'
```

移交请求是“公开写全”的普通 JSON，可存成文件再用 `requests --file` 实际导入。

## 纸叶装配与翻拍支撑（binding / photography）

在 `archive_handoff` 之上加了两块职责明确、彼此独立的说明与计算：**真支承判定**
（`binding.py` / `binding_service.py`）与**成像目标**（`photography.py`），另有一份
文档化走查 `binding_scenario.py`。两者都只消费移交侧的保管事实，不重算保管责任。

### 纸叶装配单与支承表

- 给案卷成员挂接**纸叶装配单**（`LeafAssembly`，`members` 是它所附着的成员）。
  每个相邻页缝给一组**获准的开角档**（整数级，必含闭合档 `0`）。
- **补条**（`PatchStrip`）跨一处或两处页缝，记录其允许的单级/成对档位。同一
  `leaf_group` 的所有补条必须由**同一整册姿态**同时满足——这就是整册支承表。
- 一个姿态是 `每个页缝取一个获准档`。`support_witness()` 给出全部静态安全姿态；
  `pose_witness()` 逐补条解释某姿态为何成立或冲突（冲突点名义补条与原页 `doc_id`）。
- **可达性**：一次开合只改**一处**页缝档位，且每个中间姿态都须在支承表内；不准
  抬起整册重摆。`shortest_route()` 给出逐缝的单步路线；`target_reachable()` 判可达。

例（对应走查 A）：支承表只有 `00、10、11` 时，从 `00` 想拍第二缝在 `1`，须先到
`10` 再到 `11`。若把一次新实测收紧为只许 `00、11`，`11` **静态仍安全**，但 `00→11`
一次跳两缝、两步之间没有合法中间姿态——**不可达**。

### 补条新测与保管方签认

- `PatchMeasurement` 只有在 `signer == current_custodian(case)` 时被接受
  （否则 `unsigned_measurement`）。同号同档复读幂等；异文或旧修订整批判 `conflict`。
- 送修途中（仅有 `prepare`/计划、未 `confirm`）**保管方不变**：仍按最后已确认持馆
  签认。已在原馆的设备可依本馆实测开工；**唯一的**不挡右栏的台若在外馆，在确认前
  那一幅**不能登作可拍**（走查 D）。

### 摄影台与夹手

- 摄影台有固定左右遮挡条与可移**夹手**；`clamp_slots` 是夹手数（普通机默认 `1`）。
  夹手压在某栏会**真实遮住**它；固定遮挡条所在栏永远不可见。
- 每张目标图给 `页缝 / 可读所需档 / 必见栏`。成像是**单独一单**。
- 操作（每次只改一处开角或一处夹位，`cost` 各为 1）：
  - `open_seam`：改一处页缝档位；前/后姿态都须安全；
  - `add_clamp`：在新栏**接**夹（接手）；
  - `remove_clamp`：**撤**夹，仅当剩余夹仍牵住所有启用补条；
  - `shoot`：右缝到档、姿态安全、支承齐备、必见栏可见时成像。
- **换页或换夹手须先把已开的页缝退到交接姿态 `0…0`**：普通单夹机因此只能“先退到
  `00`、再换夹、再重开”；双夹机可保持支承地**先接新夹、再撤旧夹**（两个状态各自
  安全，**接手那一步不可跳过**）。
- 每个中间姿态整册验证；一帧不成立时区分原因：`pose_unsupported` / `no_coverage` /
  `hold_occludes_must_see`（唯一接夹位也挡必见字） / `column_occluded`（固定条永久
  挡字） / `no_route`（目标在支承表内不连通）。

### 翻拍工装的选择

- **仅实现一种**可执行工装：可移的**夹手支架**（普通单夹为默认样例，双夹为同一模型
  的 `clamp_slots=2`）。
- **定档框只做分析**（`stop_frame_probe`），不作为可执行工装：它只判构件间固定安全
  角，并在不松开全册时给出“框条是否永久遮必见栏”的结论；示例里它虽能到角，但框条
  永久遮右栏，故**不能拍**（走查 C）。
- 换一台**右栏永久被遮**的备用台：任何路线都拍不到（走查 C，`column_occluded`）。

### 输出

`binding-demo` 产出 `artifacts/binding_demo.json` 与 `artifacts/work_order.json`：
**真支承单**（支承见证 + 逐缝/成对查询 + 可达性）与**成像单**（逐帧实拍原页、姿态、
可见栏、动作前态与费用、预算）分列；`refresh_status` 是本地复拍记录。
走查覆盖：A 支承表与不可达目标、B 双夹接手与“新夹位也挡字仍不可拍”、
C 备用台永久遮栏与定档框遮挡、D 保管方签认与送修途中边界。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：日期算术与边界、缺触发待定、保全半开区间与不暂停时钟、多阻止原因去重、
批次原子拒绝、产品冻结、整案校验、部分拒绝、复读/异文、保管链与重开对账、
751 文档化走查、operations 回放、200/10/500/100 规模；
纸叶支承表与可达性、补条冲突署名、保管方签认与送修边界、六缝/三档/四图上限、
单夹先退后换、双夹接手不可跳过、夹位挡字与备用台永久遮栏、逐帧一动作与实拍记录。
