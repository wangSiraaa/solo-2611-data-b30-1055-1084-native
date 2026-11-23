case.json的比分始终按该match的主/客槽位，不直接附着球队；before_fixture每对一次、主客1/2平衡，flipped_match_ids同时翻三场得到另一合法赛程。scores固定，按两个真实edition解读应不同。所有时间/赛程为虚构；不提供排名或赛程目标代码。

本微型输入的完整映射：每轮唯一可用slot为s，容量均为slot_capacity=2；blackouts=[]表示所有球队无禁用slot。scores各行为state=played的摘要，稳定result_id取r-加match_id，按数组顺序组成一次原子初始批request_id=initial-results，引用同名match_id；score_home/score_away分别取home_goals/away_goals。该事实切面固定为此批提交后，无额外判罚。

六队杯抽签见 `cup/`：由 `tests/fixtures.py` 的 6 队单循环骨架重命名为甲乙丙丁戊己，固定成绩（名次靠前者 1:0 胜）得到真实名次甲>乙>丙>丁>戊>己；前三作固定主签、后三作客队。原馆单四张完签（甲遇丁/戊/己 = 1/4、1/4、1/2），乙补充馆单允许接己后六签均等（各 1/3），已揭“甲遇丁”后乙只能抽戊。`python3 cup_demo.py` 与 `tests/test_cup_*.py` 覆盖这些数字；场馆目录、完签只按客队先后辨认、均匀整数抽签、独立重放与停电接班、无签不抽均有断言。

