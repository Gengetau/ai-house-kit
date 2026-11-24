# ai-house-kit

**填一个 API Key，在浏览器里养活一屋子有作息、会做饭换装、能坐电车去「晴空都」逛街偶遇的 AI 室友。**

> 📖 **[English README](README.en.md)** · 🧩 **[自定义世界包与插件教程](docs/CUSTOM-WORLD-TUTORIAL.md)** · 🏙️ **[插件与晴空都联动](docs/PLUGINS-AND-CITY.md)** · 💬 **[QQ 与聊天通道说明](docs/CHAT-AND-QQ.md)** · 🚀 **[后台常驻与并排部署](docs/DEPLOY.md)**

## 为什么它不一样？

- **三行命令开箱即住（零前端构建、跨 Windows / Linux）**：核心运行时仅靠 Python 3.11+ 标准库与 SQLite；不填 API Key 也能以零成本内置演示模式体验完整一天的生活，填入任意 OpenAI 兼容端点（OpenAI / Gemini / DeepSeek / Claude / 本地 Ollama）即切换真实智能体，并支持主备模型自动故障转移（`AI_HOUSE_FALLBACK_*`）。
- **挂机一整天只需百次级调用（省掉 98% 无效轮询）**：睡觉、洗漱、日常消遣由零成本 L0 规则驱动，只在需要拿主意的节点（做饭、邀约、购物、搭话、外出）预留 `Budget` 调一次模型；对话与日记生成均在内核写锁外执行，慢模型不会冻结状态面板。
- **从聊天约定到真实采购履行的闭环**：在群聊或私聊里和室友约好「帮忙买 2 个鸡蛋」，会进入 `house_intents` 并驱动 `Shopping` 外出下单采购，结合内核 `intent_evidence` / `script` 自动对账标记为 `completed`。
- **受众隔离长期记忆 & 每晚室友个人日记**：Built-in SQLite `MemoryStore` 严格隔离 `private` / `group` / `world` 三重边界（群聊默认不召回、不泄露房主私聊秘密；维护者可用 `AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS` 与 `AI_HOUSE_MEMORY_LEAK_FREE_GROUPS` 开启，见 `docs/CHAT-AND-QQ.md`；支持在工作台一键「忘掉」）；每晚 22:30 JST 自动生成第一人称个人日记（仅基于当日客观 `daylog`/`script`），23:30 JST 生成「今天的小屋」。
- **四轴插件、工具循环 & 自定义世界包 (`WorldPack`)**：内置表情贴纸（`sticker_pack`）、桌游掷骰（`dice_tabletop`）、共享书架便签（`book_nook`）三款示例插件；通过 `ai-house pack-check indie-studio-3` 一键切换 3 人独立游戏工作室模板（已随包内置）。
- **双模联动「晴空都」([bot-game-mcp](https://github.com/Gengetau/bot-game-mcp))**：
  - **模式 A（本地双容器私享）**：`docker compose --profile city up -d --build`，支持通过 `BOTGAME_HOUSE_ROSTER_JSON` 把自定义 `WorldPack` 住客名单同步给城市引擎；
  - **模式 B（公共联机晴空都，实验性）**：已接入真实内核外出门禁（`Outings.guard -> is_door_move`），支持「出门 / 偶遇 / 回家」，并提供**实验性**公共城市动作 `say` / `drift_book` / `take_book` / `world_wait`；把礼盒带回衣柜等尚未开启。

> **关于示例世界的名字**：示例世界里的「晴空都」「日和坂 / 棱光街 / 云港」等名字，用的就是公开城市 [bot-game-mcp](https://github.com/Gengetau/bot-game-mcp) 里的真实城市和片区名，方便你直接接入它；不是另一个虚构世界。

## 黄金五分钟快速开始

### 方式一：本地 Python 直接启动（零第三方依赖，需 Python 3.11+）

```sh
git clone https://github.com/Gengetau/ai-house-kit.git
cd ai-house-kit
python3 -m venv .venv && . .venv/bin/activate   # Windows: py -3 -m venv .venv 然后 .venv\Scripts\activate
python3 -m pip install -e .
ai-house init && ai-house doctor     # 自动生成强随机令牌的 .env，并检查环境/SQLite/WorldPack
ai-house up
```

- 建议装在虚拟环境里（上面第三行）：不少系统的 Python 不允许直接往系统目录里装包。以后每次开新终端先执行一次 `. .venv/bin/activate`，`ai-house` 命令才找得到。
- 核心没有第三方依赖。要接 QQ 再装 `python3 -m pip install -e ".[onebot]"`；要跑测试装 `".[dev]"`。
- 可选：把住客的聊天交给 agent 外壳（Claude Code）来跑，会话能续接、住客自己决定说不说话。装 `".[agent]"` 并设 `AI_HOUSE_BRAIN=claude`，详见 [docs/AGENT-BRAIN.md](docs/AGENT-BRAIN.md)。**这条路径用真的外壳加模拟模型端测过，还没有用真的 Claude 模型跑过。**

- 可选直接绑定内置 3 人工作室包：`ai-house init --pack indie-studio-3 --force && ai-house up`
- 也可在 `.env` 预填 `AI_HOUSE_API_KEY`、`AI_HOUSE_BASE_URL`、`AI_HOUSE_MODEL`，并用 `ai-house doctor --live-ping` 测通后再启动（或启动后直接在网页向导里填写并测试）。

浏览器打开 **`http://127.0.0.1:8780`**（若 `ai-house init` 生成了房主令牌 `AI_HOUSE_OWNER_TOKEN`，在玄关页填入即可进入）：
- **首位 AI 住客向导（`#/setup`）**：空屋首次启动不再硬塞 6 个演示角色，直接进入 **「迎接你的第一位 AI 住客」** 向导——自定义第 1 位住客的昵称、像素立绘、主题色、作息与厨艺，选择并测试 OpenAI 兼容大模型 API（或先以零配置演示模式体验），也支持一键载入 3 人 `indie-studio-3` 或 6 人 `life1` 示例世界包；入住后可在首页或管家页随时 **「+ 邀请新住客」**、编辑档案或重置小屋；
- **暖灯像素控制台与等轴互动**：浏览两层全屋像素剖面与 11 间等轴测房间，点房间让房主化身走进去当面说话、递零食，小屋的时间由内核按真实时钟推进；想在几分钟里看完三餐、日记与晴空都行程，另起一栋演示小屋 `ai-house up --virtual-clock`，在“小屋管家”页手动推进时间；
- **多板块生活与记忆管理**：覆盖 **「住客」**（此刻状态 / 衣柜换装 / 档案编辑 / 私聊与群聊）、**「日子」**（时间线 / 约定履行 / 厨房货架补货 / 晴空都行程）、**「珍藏」**（精装翻页日记 / 作用域记忆检索与遗忘 / 书架便签）与 **「管家」**（模拟步进 / 住客管理 / 大模型热更新 / MCP 接入配置），另有独立聊天页 `http://127.0.0.1:8780/chat`；
- **想在 QQ 上聊**：支持通用 OneBot v11 正向 WebSocket（协议端需自备，本项目不提供也不绑定某一家，接口要求见 [docs/DEPLOY.md](docs/DEPLOY.md)），**自动化测试用的是模拟 OneBot 服务器；真实 QQ 上只验证过房主私聊和群里被 @ 后的文字收发（各一次，范围很窄），图片语音和长时间运行还没在真号上试过**；接入风险自担，先读 [docs/CHAT-AND-QQ.md](docs/CHAT-AND-QQ.md)；后台常驻（systemd / compose）和与协议端并排部署见 [docs/DEPLOY.md](docs/DEPLOY.md)，`ai-house doctor --onebot-ping` 可检查连通。

> **默认只在本机可访问**：`ai-house up` 缺省只监听 `127.0.0.1`。要让局域网里别的设备访问，必须同时设置至少 16 位的 `AI_HOUSE_OWNER_TOKEN`（环境变量或 `.env`）再用 `--host 0.0.0.0`，不设会直接拒绝启动。`owner` 房主令牌开启全部控制与向导/管理接口；另可按需配置 `AI_HOUSE_READ_TOKEN`、`AI_HOUSE_WRITE_TOKEN`、`AI_HOUSE_PRIVATE_TOKEN`（读 / 写 / 私聊三档），脚本与 MCP 客户端统一通过 `Authorization: Bearer <令牌>` 传参。

### 方式二：Docker Compose（含可选晴空都本地双容器）

```sh
docker info                              # 先确认 Docker 已启动；报 "failed to connect" 就先把 Docker 开起来
python3 -m pip install -e .              # 只是为了用 ai-house init 生成 .env；建议在方式一的虚拟环境里执行
ai-house init                            # 生成强随机 AI_HOUSE_OWNER_TOKEN 与 CITY_BRIDGE_KEY
docker compose up -d --build             # 单容器启动，只发布 127.0.0.1:8780
```

如需拉起本地双容器晴空都：在 `.env` 设 `WORLD_KERNEL_CITY_BRIDGE=1`（按需设 `WORLD_KERNEL_CITY_ACTORS=nell` 或 `all`），再运行：

```sh
docker compose --profile city up -d --build
```

城市引擎镜像固定到 `bot-game-mcp@971484c13004e14ee6e91e9a9a75c0bd5ec65020`，仅挂在 `internal: true` 的内部网络 `citynet` 上、不暴露城市端口到宿主机；使用自定义世界包初始化时（`ai-house init --pack indie-studio-3`），还会自动写入 `BOTGAME_HOUSE_ROSTER_JSON` 让城市引擎认识你的自定义室友名单。

## 接入 MCP 客户端（Claude Desktop / Cursor / Antigravity）

启动 `ai-house up` 后，本地提供 Streamable HTTP MCP 端点（`http://127.0.0.1:8780/mcp`）：

```json
{
  "mcpServers": {
    "ai-house": {
      "url": "http://127.0.0.1:8780/mcp",
      "headers": {
        "Authorization": "Bearer <你的 AI_HOUSE_OWNER_TOKEN>"
      }
    }
  }
}
```

## 架构与插件生态

- **`src/ai_house/`**：开箱即用宿主守护进程（`daemon.py`）、首位住客与大模型初始化引擎（`onboarding.py`）、受众隔离对话与轻量约定提取（`chat_agent.py`）、四轴插件 SDK（`plugin_api.py`）、暖灯像素可视化控制台（`web/` 含两层剖面/等轴房间引擎与 `#/setup` 向导）以及内置标准 MCP 端点（`POST /mcp`）。
- **内置核心扩展**：长期记忆库（`memory.py`）、个人睡前日记（`diary.py`）、CLI 体检工具（`cli.py init/doctor`）及工具插件（`sticker_pack` / `dice_tabletop` / `book_nook`）。
- **聊天入口**：网页和 QQ（`builtin_plugins/onebot_ws.py`，可选依赖 `pip install -e ".[onebot]"`）共用 `HouseDaemon.converse`；发言人身份在 `chat_identity.py`，发送前过滤在 `chat_guard.py`；详见 [docs/CHAT-AND-QQ.md](docs/CHAT-AND-QQ.md)。
- **`world-kernel/`**：单写者事务、命令管线、R1/R2 视图、受众隔离材料、L0 日常、预算账本、轻量约定与履行（`intents_lite.py`）、衣柜与晴空都桥接客户端。
- **`world-catalog/`**：统一词表、`load_pack()`、守卫校验 `guard()` 与默认樱丘六人示例世界 `life1/`。
- **`world-contract-tests/`**：黄金契约测试与离线判定器 `oracle.py`。
- 插件开发与自定义世界包详见 **[docs/CUSTOM-WORLD-TUTORIAL.md](docs/CUSTOM-WORLD-TUTORIAL.md)** 与 **[docs/PLUGINS-AND-CITY.md](docs/PLUGINS-AND-CITY.md)**。

## 跑全套测试

```sh
python3 -m pip install -e ".[dev]"
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-kernel -p "test_*.py"
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-catalog -p "test_*.py"
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-contract-tests -p "test_*.py"
```

## 免责声明与许可证

请使用你自己的合规 API Key；本项目不提供任何账号登录代理或订阅转发。把它接到聊天平台或外部服务时，合规风险由使用者自行承担。

- 代码：[Apache License 2.0](LICENSE)
- 示例世界内容（`world-catalog/worldcatalog/life1/`、`packs/`、`src/ai_house/bundled_packs/`、`world-kernel/recordings/`）：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 第三方声明见 [NOTICE](NOTICE)
