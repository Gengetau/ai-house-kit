# Changelog

## 0.3.0

- **0.3.0 安全、隐私与可靠性全量审计加固（PR #32–#37）**：
  - **隐私与访问边界**：统一 `world / group / private` 作用域大小写规范化；看图与语音插件强制通过 `resolve_scoped_media_path` 做分域隔离；`browse_album` 与 `/api/album` 校验渠道与权限档位；`ModelRouter` 禁止主密钥跨自定义 `base_url` 泄露；收紧 Web `write` 档令牌身份边界与配置/手动外部账号同步规则；备份目录 `0o700` 建夹与敏感字段文档补齐；强化媒体网关 DNS 解析绑定、`.localhost` 拦截与 CGNAT 网段防护；私密密钥文件原子建档零窗口。
  - **记忆与约定真实凭据闭环**：群聊未核实陈述绑定单行原始说话人（防房主在场越权晋升 `fact`）与冲突降权裁决，`memory.db` 升级 v3 迁移降权历史存疑事实；词表外采购约定保留 `raw_item` 不误消；`plan_ref` 按作用域与成员分桶防串单；收紧外出/晚睡结单凭据约束。
  - **消息投递、媒体回传与数据安全**：打通 QQ 气泡统一 `SendGate` 与显式群目标路由；主动发言补全 `target_group_id`、闭麦静默拦截、先投后扣与 `expired_ttl`；断线发送失败降级持久化补发与过期丢弃、`ChannelSpool` 定期清理与 `<sep>`/代码块保真分段；`Dispatcher` 并发加锁与异常隔离；打通文生图/语音/贴纸包（含纯标准库光栅化 PNG）在 Web UI 与 OneBot 的持久化附件投递及懒加载。
  - **备份恢复、熔断限额与扩展沙箱**：改为真实挂钟备份淘汰并纳入 `runtime/media/` 归档；新增 `.daemon.lock` 实例互斥、临时目录 SHA-256/`PRAGMA integrity_check` 原子回滚恢复与 `ai-house restore` 命令；`converse` 增设 `_world_epoch` 重置守护及 `reset_house` 全量边库清理；环境变量校验异常自动容错降级不拖垮设置页与周期任务；引入端点熔断器（3 错熔断 30s）、工具循环总预算、收窄推理模型 compat 缓存与空回复重试报错；内置付费插件每日/访客每小时限额与 `usage.db` 记账；网页搜索/视觉/语音注入隔离边界，相册哈希去重、滚动上限与 `inline=0` 懒加载；提醒禁设过去时间、按频道限额与先入队后标已触发；新增 `AttachmentProcessor` 入站扩展钩子与第三方插件只读 `PluginHostProxy` 沙箱，并完成无注入 32h 回归、`gpt-6.1-terra` 真模型 32h 实测与真实 `.dist-info` 发现验证。
- **插件底座 1.1–1.3（清单、通用设置页、钩子与失败隔离）**：
  - 升级 `ai_house.plugin_api`：新增 `PluginManifest`、`ConfigField`、`PluginDefinition`、`PluginLoadErrorRecord`、`ToolRegistration`（支持 `ToolContext`）、`PluginRoute`（`/api/plugins/<plugin_id>/<subpath>`）与 `PluginTask`（周期任务钩子）；统一按插件生效与失败隔离，不因单个插件配置或导入失败拖垮宿主，并新增 `ai-house plugins` 清单与报错输出。
  - 新增 `SettingsStore`（原子写入 `plugin_settings.json`、`0o600` 密钥存储及脱敏回显）、`GET/POST /api/settings/plugins` 热重载接口，以及网页端动态 `#/settings` 插件设置页；提供独立第三方示例插件包 `examples/hello-tea-plugin`。
- **模型路由与用量账本 1.4（`ModelRouter`）**：
  - 新增 `src/ai_house/llm.py`：统一 OpenAI 兼容请求构造、GPT-5 / 推理模型参数自适配与能力缓存、`408/429/5xx` 重试与备用通道切换；支持按任务类型（`chat / life / memory_extract / diary / ping`）与按住客模型路由；记录 SQLite `usage.db` 并开放 `GET /api/usage`。
- **渠道解耦身份层 1.5（`IdentityStore`）**：
  - 新增 `src/ai_house/identity.py`：以内部身份（`owner / resident / visitor / bot`）为中心映射网页端与 OneBot 等多渠道外部账号；`seen` 表迁移 `speaker_id`；`converse`、`memory` 与 `onebot_v11` 自动跳过其他 bot 发言。
- **出站通道桥与统一发送门 1.6（`SendGate` + `outbox`）**：
  - 打通 `worldkernel.outbox`（新增 `enqueue_outbound` 与 `BlockedDelivery`）、`ChannelPlugin.deliver` / `lookup_delivery`、`OneBotChannel` 真发/断线 defer 与超时对账语义；新增 `SendGate` 统一执行敏感词与私聊防泄漏拦截、bot 拒发、JST 静默时段及小时/日频率上限，并开放 `GET /api/outbox` 与 `POST /api/outbox/flush`。
- **阶段 2 纯逻辑能力 2.1–2.8**：
  - **2.1 通道可靠性（`ChannelSpool` + `OneBotChannel`）**：新增 `channel_spool.db` 入站落盘、事件 ID 去重、防抖聚合、自动分段、仅写前失败退避重试与重启恢复补发。
  - **2.2 群聊行为与睡眠静默（`GroupBehaviorController` + `group_behavior`）**：新增非房主回复概率、连续回复上限与静默期清零、房主语音/HTTP 闭麦与住客熟睡静默控制。
  - **2.3 事件驱动主动发言（`ProactiveEngine` + `proactive_talk`）**：默认关闭按需启用；锚定游标不重放历史、无事件不冷场、调用模型前执行 JST 活跃时段窗与全屋/单人配额拦截。
  - **2.4 记忆原话证据与防外泄（`MemoryStore`）**：新增 `claim` 与 `trust` 可信度分级、群聊成员单方面陈述降级降权、拒绝纯提问/本人猜测写入 `fact`，并加强跨范围私密过敏实体防泄漏检测。
  - **2.5 完整约定系统（`CommitmentStore`）**：房主立约权限闸、多人共同计划共享 `plan_ref` 与 `ChatLink` 兼容、过敏忌口归 `memo` 防误消防过期、`intent_evidence` 严格收口与 `/api/intents/cancel`。
  - **2.6 提醒与无状态小游戏插件（`reminders` + `mini_games`）**：基于 `reminders.db` 的作用域隔离定时提醒与 `urgent` Outbox 推送，以及零副作用猜拳/抽签/随机选/接龙工具。
  - **2.7 模型用量账本页（`#/usage`）**：支持 `today / 7d / 30d / all` 多周期聚合与最近调用明细，遵循容器查询与无障碍表格规范（`role="region"`、`tabindex="0"`、吸顶表头/吸左列与窄容器卡片堆叠）。
  - **2.8 在线热备与 32 小时回归（`ai-house backup`）**：SQLite `Connection.backup()` 热备、`PRAGMA integrity_check`、SHA-256 `manifest.json`（不打包 `.env`）、保留裁剪与恢复检验，及 32 虚拟小时契约回归（含试住问题 #2 与 #4）。
- **阶段 3 可选服务插件 3.1–3.6（默认全部关闭）**：
  - **3.1 看图（`vision_understand`）**：OpenAI 兼容多模态图片理解与 `media_safety` SSRF/字节限制网关；接通网页端与 OneBot 图片段自动看图，未配/故障静默降级不断聊。
  - **3.2 语音转文字与语音合成（`voice_io`）**：纯标准库 `multipart/form-data` 调用 `/v1/audio/transcriptions` 与经发前隐私审查的 `/v1/audio/speech`（自带 `wave` 头解析），接通 OneBot 语音入站转写与 `[record]` 发送。
  - **3.3 文生图与自带引擎接口（`image_gen`）**：支持 OpenAI `/v1/images/generations`、自建 HTTP JSON 服务协议（`byo_http`）与内存 Python 回调接口（`custom_python`），发前执行隐私/关键词审查并落盘 `runtime/media/generated/`。
  - **3.4 联网搜索（`web_search`）**：可选 SearXNG / Tavily / Brave / 自定义 JSON 接口，出站先拦私密记忆与敏感词，入站剥离 HTML 与提示词注入伪装。
  - **3.5 表情包库（`meme_library` + `bundled_stickers/`）**：附带 8 张原创 CC0-1.0 暖灯风 SVG 贴纸及许可证/作者校验，防止未署名与越界路径加载，并支持 OneBot `[image]` 气泡发送。
  - **3.6 相册与日记配图（`album_diary_art` + `AlbumStore`）**：严格按 `world` / `group` / `private` 分域隔离 `album.db`（私聊照片绝不进入公共相册与日记），支持晚间日记自动配图静默降级及控制台 `#/keep/album` 相册展示与翻书插画。

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
