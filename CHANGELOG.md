# Changelog

## 0.2.0

- **暖灯像素控制台（Warm Lamp Console）与首位 AI 住客向导**：
  - 全新启动 `ai-house up`（未显式指定 `--pack` 且运行目录为空时）直接进入 `#/setup`「迎接你的第一位 AI 住客」向导：自由创建第 1 位住客（昵称、英文 ID 建议、9 套内置像素立绘与主题色、性格签名、作息与厨艺预设），支持在网页端填写并实测 OpenAI 兼容大模型 API（写入 `.env` 并热重载）或以零配置演示模式起步，也保留一键载入 3 人 `indie-studio-3` / 6 人 `life1` 示例包入口。
  - 提供两层全屋像素剖面与 11 间等轴测房间内景（支持房主化身走入房间互动）、五板块可视化工作台（`小屋`/`走进像素小屋`/`住客`此刻·衣柜·档案·私聊群聊/`日子`时间线·约定·厨房货架·晴空都/`珍藏`翻页日记·记忆遗忘·书架便签/`管家`步进·住客增编·大模型热更新·MCP 配置·重置小屋）以及独立聊天页 `/chat`。
- **CLI 与安全底座**：
  - 新增 `ai-house init`（自动生成高强度随机 `AI_HOUSE_OWNER_TOKEN` 与 `CITY_BRIDGE_KEY`）、`ai-house doctor`（支持 `--live-ping`）、`ai-house pack-check` 与 `ai-house plugins`；随包内置 `indie-studio-3` 世界包。
  - 默认仅监听 `127.0.0.1`；绑定非回环地址必须配置至少 16 位的 `AI_HOUSE_OWNER_TOKEN`；网页、REST 与 `/mcp` 统一采用 `Authorization: Bearer` 鉴权（禁止 `?token=` URL 传参），细分 `owner`（房主管理/向导/重置）及 `read / write / private` 权限档位；修复 Windows / Linux 下 SQLite 短连接与退出句柄清理。
- **对话大脑、长期记忆、个人日记与采购闭环**：
  - 统一 `HouseDaemon.converse` 对话入口与 `chat_guard.py` 发前泄露/提示词/敏感路径拦截，大模型调用期间释放状态锁避免前端卡顿，并支持 `AI_HOUSE_FALLBACK_*` 主备故障转移、有界 `tool_calls` 循环与 3 款内置示例工具（`sticker_pack`、`dice_tabletop`、`book_nook`）。
  - 接入 SQLite `MemoryStore`（严格隔离 `private / group / world` 边界并支持一键忘掉）、每晚 `22:30 JST` `DiaryService` 第一人称个人日记、以及 `IntentsLite` 聊天约定触发 `Shopping` 采购对账收口闭环。
  - 新增可选 OneBot v11 正向 WebSocket 通道 `builtin_plugins/onebot_ws.py`（可选依赖 `pip install -e ".[onebot]"`，仅在模拟 OneBot 服务器测试通过，未在真实 QQ 上测试）。
- **四轴插件 SDK、Streamable MCP 与晴空都双模联动**：
  - 新增 `ai_house.plugin_api`（`WorldPack`、`ChannelPlugin`、`LifeExtension`、`StudioProvider`）与 `POST /mcp` 标准 Streamable HTTP MCP 服务端；消除内核硬编码六人常量以兼容任意自定义 `WorldPack`。
  - 同步晴空都 R0 Schema（`hiyorizaka / ryokogai / kumominato`）与 `mappings v3`：`docker compose --profile city` 支持内部网络 `citynet` 双容器私享启动（引擎固定为 `bot-game-mcp@971484c13004e14ee6e91e9a9a75c0bd5ec65020` 并通过 `BOTGAME_HOUSE_ROSTER_JSON` 同步自定义名单）；公共晴空都 MCP 远足扩展接通真实内核门禁（`Outings.guard -> is_door_move`）与实验性 `say`/`drift_book`/`take_book`/`world_wait`。

## 0.1.0（预览版）

- 首个公开预览：world-kernel、world-catalog、world-contract-tests 和虚构示例世界。
