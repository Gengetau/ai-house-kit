# ai-house-kit

让几个 AI 角色住进同一个虚拟的家、各自过日子的**世界内核**，以及一个可以直接跑起来的虚构示例世界。

> English summary: a small, dependency-free world kernel (Python standard library + SQLite) for simulating
> several AI characters living together in one house: single-writer transactions, a command pipeline with
> idempotency and resource reservation, audience-scoped views and materials, a rule-based daily routine
> (no model needed), a virtual model-call budget, and an outbox with receipts. Ships with a fictional example
> world and a contract test suite. Status: 0.1 preview, APIs will change.

## 它是什么

- **world-kernel/**：内核本体（`worldkernel` 包）
  - 存储与事务：SQLite 单写者锁 + runtime epoch，被接管的旧调度器什么都写不进；虚拟时钟
  - 命令管线：来源身份、幂等键、词表与时效校验、多资源稳定顺序预留、开始/完成复核，状态、效果、事件、观察者和视图在同一个事务里提交
  - 视图：按受众独立的事件序号和游标，快照（R1）与事件流（R2）同一读边界，字段白名单
  - 受众隔离的材料：谁知道一件事和这件事能对谁说分开存；悄悄话只有知情人有原文
  - L0 日常：不调用模型的需求检查（睡觉、吃饭、休息），习惯从人设确定性派生
  - 认知适配器：上下文只装角色本人感知到的东西，先记预算再调用，过时的答复直接失败
  - 虚拟预算、outbox + 回执对账、钱包、购物与外出、衣柜、每日记录等生活模块
  - 可选的城市桥接（`city_bridge.py`）：接 [bot-game-mcp](https://github.com/Gengetau/bot-game-mcp) 的城市引擎；测试里用假引擎
- **world-catalog/**：活动目录和统一词表（`worldcatalog` 包），内核在动作开始、排队和结算前都跑目录的 `guard()`
- **world-contract-tests/**：21 个黄金场景和判定器，用来验收任何实现了同一契约的内核
- **docs/city/bridge-v1.schema.json**：城市桥接接口的 JSON Schema（来自 bot-game-mcp，MIT）

## 示例世界

`world-catalog/worldcatalog/life1/` 是一个完全虚构的世界：樱丘一带的一栋小楼，六位室友（alma、elsa、mina、nell、wanda、yuki），
附近的超市、商店街、河岸和几个一日游去处，以及食材、菜谱、零食、衣物和日程。物价按日元设定。
你可以照着它的格式换成自己的世界和角色。

## 快速开始

需要 Python 3.11 或更新版本。内核本身只用标准库；城市桥接的测试（`test_city_m1a.py`）要用 `jsonschema` 校验接口，先装一下：

```sh
python3 -m pip install -r requirements-dev.txt
```

```sh
# 内核测试（约 2–3 分钟）
cd world-kernel && python3 -m unittest

# 目录测试
cd world-catalog && PYTHONPATH=. python3 -m unittest discover -s . -p 'test_*.py'

# 契约测试
cd world-contract-tests && PYTHONPATH=../world-catalog python3 -m unittest discover -s . -p 'test_*.py'

# 跑一晚合成的「厨房一夜」，导出快照、事件分页和时间线
cd world-kernel && python3 kitchen_night.py --out /tmp/kitchen-night
```

所有测试和示例都只用合成数据和假模型：不联网、不调用付费接口、不需要任何账号。

## 接入真实模型

内核本身不带任何模型后端。`shadow.py`、`authority.py` 和 `worldkernel/daynote.py` 等需要真实模型的地方，
都通过宿主程序传进来的聊天函数调用模型。`shadow.py` 和 `authority.py` 自带的函数会调用任意兼容 OpenAI 的
`/v1/chat/completions` 接口，用三个环境变量配置：

| 变量 | 作用 |
| --- | --- |
| `WORLD_KERNEL_BASE_URL` | 接口地址，默认 `http://127.0.0.1:8000` |
| `WORLD_KERNEL_API_KEY` | 你自己的 API key（必填，只在发请求时读取） |
| `WORLD_KERNEL_MODEL` | 模型名 |

请使用你自己的 API key 和服务条款允许的接入方式。本项目不提供、也不支持任何账号登录代理或订阅转发。

## 不包含的东西

- 聊天平台适配（QQ / OneBot 等）、网页控制台、记忆和日记服务、协作看板
- 任何真实角色的人设、图片或聊天记录

## 免责声明

本项目按原样提供。把它接到聊天平台或模型服务时，账号和合规风险由使用者自行承担。

## 许可证

- 代码：[Apache License 2.0](LICENSE)
- 示例世界的内容（`world-catalog/worldcatalog/life1/`、`world-kernel/recordings/`）：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 第三方声明见 [NOTICE](NOTICE)

Copyright 2026 Gengetau
