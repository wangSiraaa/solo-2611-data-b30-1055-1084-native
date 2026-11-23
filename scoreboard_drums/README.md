# 联赛馆换牌台：数字鼓交班核对与遮幕逐拍规划

虚构联赛馆里两只**只会向前转**的十进制数字鼓（每只一位，9 再走一格到 0），
按实物牌号 A/B 标识，换主队不清零。系统解决两件事：

1. **交班校验**：电工凭上次亲眼见到的鼓面、发给指定鼓的脉冲（有回执/失回执）、
   独立齿位触片读数，按先后核出全部可能的联合止位；矛盾时指出不能同时成立的
   交班记录；同一次总闸失电脉冲组要么全走、要么全丢。
2. **遮幕换牌**：播报员对指定版次的真实已完赛比分发换牌单（按该版主客映射
   取数，待赛拒播）；落幕前显示旧牌，幕内每拍只许走一格或读一只鼓，要在
   六拍遮幕时限内对交班单允许的每种真相都可执行，否则留幕不发。

## 目录与运行

```
scoreboard_drums/
├── scoreboard/
│   ├── core.py        # 鼓/证据/止位领域模型，只向前转的十进制规则
│   ├── reconcile.py   # 交班核对：真相枚举 + 触片过滤 + 贪心矛盾核
│   ├── planner.py     # 信念集上的有界策略树（最坏拍数最优，含独立剪枝）
│   ├── cards.py       # 电工席逐拍操作卡：纯 JSON 数据 + 断链执行器
│   ├── controller.py  # 馆内控制器：止位信念、逐拍决策、换目标重算、交班快照
│   ├── broadcast.py   # 版次主客映射、真实赛果录入、待赛拒绝播报
│   ├── replay.py      # 每个止位独立回放，产出现场记录
│   └── scenarios.py   # 题面五组情形 + 矛盾构造 + 播报席构造
├── tests/
│   ├── oracle.py      # 独立第二实现：minimax 暴力枚举最小最坏拍数
│   └── test_*.py      # 38 个 unittest（不依赖网络/外部服务）
├── demo.py            # 端到端演示，产物落 run_data/
└── run_data/          # 各情形现场记录 JSON 与操作卡（可删除重建）
```

```bash
python3 demo.py                       # 端到端演示
python3 -m unittest discover -s tests # 全部测试
```

## 题面断言的落点

| 题面情形 | 系统结论 | 对应测试 |
| --- | --- | --- |
| 录入 2:2，交班认定 (1,1)，遮两拍 | 可发，最坏 2 拍 | `test_a_two_beats_issuable_for_22` |
| 客鼓最后一下失回执，可能 (1,1)/(1,2) | 两拍不能保证；三拍先读客鼓再分叉 | `test_b_two_beats_cannot_guarantee_but_three_reads_away_first` |
| 仍认定 (1,1)，遮幕改成一拍 | 留幕不发；放宽两拍恢复可发 | `test_c_one_beat_curtain_refuses_even_though_certain` |
| 9 转 0 | 回绕一格即到，两拍可发 | `test_d_wraparound_9_to_0_is_one_step_each` |
| 总闸失电脉冲组 | 只产生成对止位 (1,1)/(2,2)，无交叉 | `test_breaker_group_is_atomic_paired_stops` |
| 主客换版 | 比分事实不变，目标鼓随版次映射改变 | `test_edition_swap_flips_drum_mapping_but_not_facts` |
| 待赛播报 | 拒绝签发换牌单 | `test_pending_match_refuses_broadcast` |
| 矛盾交班 | 贪心最小矛盾核（脉冲 + 触片），无止位 | `test_contradiction_locates_incompatible_records` |

## 两种现场交付（断开与电工席的连接后均可用）

- **操作卡（cards.py）**：电工席把策略树压成带页码的纯 JSON，每页一个动作，
  读头页按读数翻页。现场执行器只吃 JSON + 本地硬件，无任何网络依赖；
  临时换目标必须换卡，卡头（比赛/版次/目标）不符直接拒绝。
- **馆内控制器（controller.py）**：保管剩余止位与策略树，随读头更新信念；
  换目标在馆内按当前信念重算，不清零已花拍数；换班交接已花拍数、真实动作、
  剩余止位（`to_json`/`from_handover` 可落盘恢复）。
