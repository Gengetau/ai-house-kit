# 厨房一晚（合成回放）

世界 `synthetic-kitchen-night`，18:00–07:30 JST，假模型、零付费。模型调用 6 次（预算上限 60）。

决策点：called 5，retry 1，routine_action 17，would_call 5

| 时间 | 谁 | 在哪 | 发生了什么 |
| --- | --- | --- | --- |
| 18:00:00 | - | kitchen | 引导：dinner |
| 18:00:00 | mina | living | 开始 eat（eating，routine） |
| 18:00:00 | yuki | exit_rooms | 开始 rest（resting，routine） |
| 18:00:00 | nell | - | ※ 想了想（dinner，假模型） |
| 18:00:00 | wanda | - | ※ 想了想（hungry，假模型） |
| 18:00:30 | nell | kitchen | 开始 cook（cooking，model） |
| 18:00:30 | - | kitchen | scene_updated |
| 18:00:30 | wanda | - | ※ 决策点 hungry：stale:resource_unavailable → retry |
| 18:01:00 | wanda | living | 开始 move（walking，model） |
| 18:01:00 | - | kitchen | scene_updated |
| 18:01:08 | wanda | living | 出发去 kitchen |
| 18:01:08 | wanda | kitchen | 到了 |
| 18:10:00 | nell | kitchen | phone_notified |
| 18:10:00 | mina | living | 完成 eat |
| 18:10:00 | nell | - | ※ 手机震了一下（正文未读） |
| 18:15:30 | nell | kitchen | 完成 cook |
| 18:15:30 | nell | - | ※ 想了想（soup_ready，假模型） |
| 18:16:00 | nell | kitchen | 说：汤好了，一起喝吧？（对 wanda） |
| 18:16:00 | nell | kitchen | 开始 eat（eating，model） |
| 18:16:00 | wanda | - | ※ 想了想（invited，假模型） |
| 18:16:30 | wanda | kitchen | 开始 eat（eating，model） |
| 18:20:00 | yuki | exit_rooms | 完成 rest |
| 18:26:00 | nell | kitchen | 完成 eat |
| 18:26:00 | nell | kitchen | phone_read |
| 18:26:00 | nell | - | ※ 空下手来看了手机（succeeded） |
| 18:26:30 | wanda | kitchen | 完成 eat |
| 18:30:00 | - | kitchen | scene_updated |
| 18:30:00 | - | kitchen | 引导：dinner |
| 19:00:00 | - | kitchen | owner_entered |
| 19:00:00 | - | kitchen | scene_updated |
| 19:00:00 | - | kitchen | 引导：dinner |
| 19:00:00 | owner | - | ※ 房主化身进了厨房 |
| 19:05:00 | - | kitchen | owner_input_received |
| 19:05:00 | owner | - | ※ 房主说：好香啊 |
| 19:20:00 | owner | - | ※ 客户端断线（心跳停止） |
| 19:20:30 | - | kitchen | owner_presence_unknown |
| 19:30:00 | - | kitchen | scene_updated |
| 19:30:00 | - | kitchen | 引导：dinner |
| 19:40:30 | yuki | exit_rooms | 开始 rest（resting，routine） |
| 20:00:00 | - | kitchen | scene_updated |
| 20:00:00 | - | kitchen | 引导：dinner |
| 20:00:30 | yuki | exit_rooms | 完成 rest |
| 20:30:00 | - | kitchen | scene_updated |
| 20:30:00 | - | living | 引导：evening_living |
| 21:00:00 | - | living | scene_updated |
| 21:00:00 | - | living | 引导：evening_living |
| 21:30:00 | - | living | scene_updated |
| 21:30:00 | - | living | 引导：evening_living |
| 22:00:00 | - | living | scene_updated |
| 22:00:00 | - | living | 引导：evening_living |
| 22:10:30 | alma | - | ※ 决策点 hunger：hungry_no_ready_food → would_call |
| 22:30:00 | - | living | scene_updated |
| 22:30:00 | - | living | 引导：evening_living |
| 22:30:00 | alma | living | 开始 move（walking，routine） |
| 22:30:12 | alma | living | 出发去 exit_rooms |
| 22:30:12 | alma | exit_rooms | 到了 |
| 22:30:30 | alma | exit_rooms | 开始 sleep（sleeping，routine） |
| 23:00:00 | - | living | scene_updated |
| 23:00:00 | elsa | living | 开始 move（walking，routine） |
| 23:00:12 | elsa | living | 出发去 exit_rooms |
| 23:00:12 | elsa | exit_rooms | 到了 |
| 23:00:30 | elsa | exit_rooms | 开始 sleep（sleeping，routine） |
| 23:00:30 | yuki | exit_rooms | 开始 rest（resting，routine） |
| 23:20:30 | yuki | exit_rooms | 完成 rest |
| 23:30:00 | nell | kitchen | 开始 move（walking，routine） |
| 23:30:08 | nell | kitchen | 出发去 living |
| 23:30:08 | nell | living | 到了 |
| 23:30:30 | nell | living | 开始 move（walking，routine） |
| 23:30:42 | nell | living | 出发去 exit_rooms |
| 23:30:42 | nell | exit_rooms | 到了 |
| 23:31:00 | nell | exit_rooms | 开始 sleep（sleeping，routine） |
| 00:30:00 | wanda | kitchen | 开始 move（walking，routine） |
| 00:30:08 | wanda | kitchen | 出发去 living |
| 00:30:08 | wanda | living | 到了 |
| 00:30:30 | wanda | living | 开始 move（walking，routine） |
| 00:30:42 | wanda | living | 出发去 exit_rooms |
| 00:30:42 | wanda | exit_rooms | 到了 |
| 00:31:00 | wanda | exit_rooms | 开始 sleep（sleeping，routine） |
| 00:40:30 | yuki | - | ※ 决策点 hunger：hungry_no_ready_food → would_call |
| 01:00:00 | yuki | exit_rooms | 开始 sleep（sleeping，routine） |
| 03:30:00 | mina | living | 开始 move（walking，routine） |
| 03:30:12 | mina | living | 出发去 exit_rooms |
| 03:30:12 | mina | exit_rooms | 到了 |
| 03:30:30 | mina | exit_rooms | 开始 sleep（sleeping，routine） |
| 06:30:30 | alma | exit_rooms | 完成 sleep |
| 06:30:30 | alma | - | ※ 决策点 hunger：hungry_no_ready_food → would_call |
| 07:00:30 | elsa | exit_rooms | 完成 sleep |
| 07:00:30 | elsa | - | ※ 决策点 hunger：hungry_no_ready_food → would_call |
| 07:30:00 | nell | exit_rooms | 完成 sleep |
| 07:30:00 | nell | - | ※ 决策点 hunger：hungry_no_ready_food → would_call |

## 早上 07:30

- nell：exit_rooms，idle
- elsa：exit_rooms，idle
- alma：exit_rooms，idle
- mina：exit_rooms，sleeping
- yuki：exit_rooms，sleeping
- wanda：exit_rooms，sleeping
