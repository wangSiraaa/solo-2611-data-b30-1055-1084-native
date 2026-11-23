case.json的比分始终按该match的主/客槽位，不直接附着球队；before_fixture每对一次、主客1/2平衡，flipped_match_ids同时翻三场得到另一合法赛程。scores固定，按两个真实edition解读应不同。所有时间/赛程为虚构；不提供排名或赛程目标代码。

本微型输入的完整映射：每轮唯一可用slot为s，容量均为slot_capacity=2；blackouts=[]表示所有球队无禁用slot。scores各行为state=played的摘要，稳定result_id取r-加match_id，按数组顺序组成一次原子初始批request_id=initial-results，引用同名match_id；score_home/score_away分别取home_goals/away_goals。该事实切面固定为此批提交后，无额外判罚。

## 洗衣规划子系统（laundry/）

在旧仓之上新增只读消费方：从已发布 edition 与 played/cancelled 事实派生待洗布件，
按“约束→允许程序集合”鉴定每件/整袋洗涤程序，并在实筒/容量/用时/截止下给出分袋、
整筒、交回方案或缺件、时窗不足的理由。两法（方案先行/实筒先行）、坏筒失效、已开机
重开、同比赛改报取消改变待洗件均有实现与测试。详见 `LAUNDRY_DESIGN.md`；
运行 `python3 demo_laundry.py`。
