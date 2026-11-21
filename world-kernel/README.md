# world-kernel

世界内核。纯 Python 标准库 + SQLite；目录来自同仓库的 `world-catalog/`（也可以用环境变量 `WORLD_CATALOG_DIR` 指定）。

| 模块 | 内容 |
| --- | --- |
| `worldkernel/store.py` | 表结构、短写事务、显式读事务、虚拟时钟、单写者锁 + runtime_epoch、world_id 隔离 |
| `worldkernel/kernel.py` | 命令管线：来源身份、幂等键 + 请求摘要、词表/时效/版本校验、多资源稳定顺序预留、开始/完成复核、同事务提交 |
| `worldkernel/views.py` | game_view：受众独立序号、游标、R1 快照与 R2 事件同一读边界、410、字段白名单 |
| `worldkernel/guide.py` | 规则生成场景邀约（晚饭、客厅夜间），不替角色决定出席和结局 |
| `worldkernel/routine.py`、`habits.py` | L0 日常：无模型的需求检查和从人设派生的习惯 |
| `worldkernel/cognition.py` | 认知适配器：只装角色本人感知到的上下文，先记预算再调用，过时答复失败 |
| `worldkernel/budget.py` | 虚拟调用额度：调用前持久预留，满了就降级不等待 |
| `worldkernel/outbox.py` | outbox + 回执：投递和事实同事务登记，超时记 unknown，按 delivery_id 对账 |
| `worldkernel/materials.py` | 按受众隔离的材料：knowers 与 disclosure_scope 分开 |
| `worldkernel/life.py`、`life_model.py` | 生活层：一天的作息、吃饭、外出、邀约和模型决策点 |
| `worldkernel/wallet.py`、`shopping.py`、`inventory.py`、`meals.py` | 钱包、采购、库存、做饭和吃饭 |
| `worldkernel/wardrobe*.py` | 衣柜、穿搭和可选的换装渲染队列 |
| `worldkernel/city_*.py` | 可选的城市桥接（对接 bot-game-mcp 城市引擎） |
| `worldkernel/legacy.py`、`statuscard.py`、`daylog.py`、`daynote.py` | 给宿主程序的投影、状态卡和每日记录 |

其他脚本：

- `contract_adapter.py`：把 `world-contract-tests/` 的用例映射到内核入口，导出 traces 给判定器
- `kitchen_night.py`：跑一晚合成的「厨房一夜」，导出 R1 快照、R2 分页、时间线和带 sha256 的 manifest；同 seed 重跑字节一致
- `simulate.py`：快进模拟若干天
- `shadow.py`、`authority.py`：在宿主程序里运行内核的两种方式（影子模式和权威模式），需要宿主提供聊天函数

## 跑测试

```sh
python3 -m unittest
```
