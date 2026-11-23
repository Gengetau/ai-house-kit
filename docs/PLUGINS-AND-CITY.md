# 四轴插件生态与「晴空都」联动指南

`ai-house-kit` 保持底层 `worldkernel` **100% Python 标准库零依赖**与**单写者 SQLite 事务不变量**，同时在 `ai_house.plugin_api` 中开放四大标准扩展插槽。

## 1. 四轴插件体系

| 插槽轴向 | Entry Point 组 / 目录 | 说明 |
| --- | --- | --- |
| **WorldPack（世界与室友包）** | `packs/<name>/`、`src/ai_house/bundled_packs/` 或 `ai_house.packs` | 纯 JSON 与素材目录，零 Python 代码即可定义任意人数（1～12 人）、性格提示词、做饭顺位、衣柜单品与房间拓扑。内置 `life1`（6 人洋馆）与 `indie-studio-3`（3 人独立游戏工作室）。 |
| **ChannelPlugin（聊天通道）** | `ai_house.channels` | 内置网页群聊/私聊、标准 `/mcp` 端点与通用 OneBot v11 正向 WebSocket 通道（`onebot_v11`，需安装 `.[onebot]`）。扩展时请自行做来源鉴权，并严格隔离群聊与私聊作用域。 |
| **LifeExtension（生活与世界扩展）** | `ai_house.extensions` | 实现 `step() / owns(actor) / hold_until(actor, now) / context(actor, kind) / shops`（可选 `is_door_move`）即可挂载新日常机制，例如内置 `book_nook` 与 `city_mcp_outing`。 |
| **StudioProvider（换装渲染与记忆）** | `ai_house.providers` | 幂等消费 `wardrobe_render_outbox`（内置 `svg_wardrobe`）或接入 `AI_HOUSE_MEMORY_EMBED_URL` 构建语义向量索引；聊天工具则通过 `AI_HOUSE_TOOLS` 挂载（内置 `sticker_pack`、`dice_tabletop`、`book_nook`）。 |

第三方包只需在自己的 `pyproject.toml` 声明入口点，安装进同一环境即会被 `ai-house plugins` 自动发现；参考完整示例包 `examples/hello-tea-plugin`：

```toml
[project.entry-points."ai_house.bundles"]
hello_tea = "hello_tea_plugin:PLUGIN_DEF"
```

### 0.2.x → 0.3.0 插件迁移说明

- **推荐统一导出 `PluginDefinition`（`PLUGIN_DEF`）并挂载到 `ai_house.bundles`**（旧四轴独立 Entry Point 仍作发现与降级兼容，但建议迁至单包声明）：
  - 用 `PluginManifest` + `ConfigField`（最多 8 个字段，标注 `secret=True` 的字段不会回显明文并自动纳入 `ReplyGuard`）声明配置；
  - `channel_factory` / `extension_factory` / `provider_factory` 按配置按需启用，配置缺失或抛出 `ChannelConfigError` 时隔离记录到 `load_errors`，不阻断宿主启动；
  - 聊天工具迁移到 `ToolRegistration(..., takes_context=True)` 即可注入 `ToolContext`（`actor / scope / channel / speaker / now / plugin_config / daemon`）；
  - 可额外声明 `PluginRoute`（挂载 `/api/plugins/<plugin_id>/<subpath>`）与 `PluginTask`（周期任务）；
  - 实现 `ChannelPlugin.deliver(delivery_id, payload)` 与 `lookup_delivery(delivery_id)` 即可接入 `outbox.Dispatcher` 与 `SendGate`：发前失败抛 `worldkernel.outbox.NotSent`，发送后超时或断线抛 `TimeoutError`（保持 `unknown` 待对账，不盲目重发）。

### 第三方插件拿到的宿主句柄（`PluginHostProxy`）

第三方（非内置）插件在 `ctx.daemon`、附件处理器、通道与 Provider 的 `host` 上拿到的是 `PluginHostProxy`，不是真实守护进程。它是 API 边界而不是沙箱（插件代码与宿主同进程），只保证公开接口不越权：

- **环境变量是白名单**：`proxy.env` 只含本插件在 `ConfigField.env_var` 里声明过的变量、`TZ` / `LANG` 等语言时区变量，以及名称和取值都不像凭据的 `AI_HOUSE_*` 设置。要读哪个环境变量就先在清单里声明；宿主自己的 `AI_HOUSE_*` 密钥即使被声明也不会给。本插件自己的配置（含密钥字段）用 `proxy.config()` 读取。
- **工厂函数和配置解析用同一份白名单**：第三方插件的 `channel_factory(env)` / `provider_factory(env=...)` / 扩展工厂收到的 `env`，以及从环境变量解析出来的配置值，都只来自上面这份白名单。清单里把 `env_var` 写成宿主的 `AI_HOUSE_API_KEY` 之类并不能拿到它。第三方插件自己的密钥请用自己的前缀命名（如 `MYPLUGIN_API_KEY`），不要放进 `AI_HOUSE_*` 命名空间；在设置页里填写的值不受这条限制。
- **私聊范围只给通道插件**：`list_chat("private:*")`、`scope="private"` 的 `list_memories` / `list_album_view` / `album.list_items` 只对注册了 `channel_factory` 的插件开放，其它插件会收到 `PermissionError`。`converse("private:<室友>")` 要求说话人经身份库确认为房主，其它角色（含 BOT）一律拒绝。
- **配额只用自己的**：`check_plugin_quota` / `record_plugin_call` 一律作用于代理自己的 `plugin_id`，指名别的插件会被拒绝；没有插件 id 的代理不能使用配额。`daily_limit` 不传表示默认 60，传 0 表示今日不允许调用（设置页最小值为 1）。
- **通道发送要先判定再记账**：`authorize_send(...)` 只判定，不计数；消息真正发出后必须调用 `record_sent(delivery_id, key)`（`key` 为 `authorize_send` 的返回值，只能记到自己通道名下），每小时与每日上限才会累计，否则永远不会触发限流：

```python
from worldkernel.outbox import BlockedDelivery, NotSent

try:
    key = self.host.authorize_send(self, kind="private", target=uid, actor=actor, text=reply, delivery_id=did)
except (BlockedDelivery, NotSent):
    return                      # 被发送门拦下，不发
self._send_over_the_wire(uid, reply)     # 你自己的实际发送
self.host.record_sent(did, key)          # 发送成功后记账；发送失败则不记
```

## 2. 访问控制

宿主默认只监听 `127.0.0.1`。监听其他地址必须设置至少 16 位的 `AI_HOUSE_OWNER_TOKEN`，否则拒绝启动。网页、REST 与 `/mcp` 都验令牌，分三档：

- **读**：状态、食材、群聊历史、事件流；
- **写**：房主化身、补货、出门、向群聊发言；
- **私聊**：读取或发送与某位室友的私聊（单独一档）。

小屋的时间归内核管：按真实时钟运行时，任何令牌都不能补步或快进（`POST /api/step` 返回 409）。只有用 `--virtual-clock` 启动的演示小屋接受手动推进，而且只认房主令牌。

房主令牌开全部三档；`AI_HOUSE_READ_TOKEN` / `AI_HOUSE_WRITE_TOKEN` / `AI_HOUSE_PRIVATE_TOKEN` 各开一档。没有令牌时只能从本机访问，并拒绝非回环的 Host 头和跨站 POST。令牌用 `Authorization: Bearer <令牌>`（或 `X-AI-House-Token`）传递，任何接口都不接受放在 URL 里的 `?token=`（代理和访问日志会留存 URL）；网页的事件流用带请求头的 fetch 流订阅。

补充接口与权限对应：
- `GET /api/diaries` 与 `GET /api/memories?scope=world|group` 需 **读** 档；
- `GET /api/memories?scope=private&actor=...`、`POST /api/memories/forget`（`scope=private`）与 `GET /api/intents` 需 **私聊** 档；
- `POST /api/memories/forget`（`scope=world|group`）需 **写** 档。

## 3. 与「晴空都」联动

示例世界里的城市名和片区名（晴空都、日和坂 / 棱光街 / 云港）用的是公开城市 [bot-game-mcp](https://github.com/Gengetau/bot-game-mcp) 里的真实名字。

### 模式 A：本地私享城市（`city-bridge/1` 桥接）

需要自己运行城市引擎。`docker compose --profile city up -d --build` 会构建并启动固定在 `bot-game-mcp main@971484c13004e14ee6e91e9a9a75c0bd5ec65020` 的引擎容器：它只挂 compose 内部网络，不对宿主发布端口。在 `.env` 里设置：

```ini
CITY_BRIDGE_KEY=<独立的随机串，至少 24 位，不能和房主令牌相同>
WORLD_KERNEL_CITY_BRIDGE=1
WORLD_KERNEL_CITY_ACTORS=nell      # 默认没有室友进城；写 all 才是全员
CITY_PROFILE=qingkong
```

`WORLD_KERNEL_CITY_ACTORS` 与 `WORLD_KERNEL_CITY_PLANS`（是否让室友的外出计划自动走城市）缺省都是关闭。打开桥却没配桥密钥、桥密钥太短、是占位串、或与任何一个访问令牌相同时，小屋一侧会拒绝启动；引擎容器在桥密钥缺失、是占位串或短于 24 位时拒绝启动（它看不到房主令牌，所以「不与令牌相同」只由小屋一侧检查）。回家结算目前只带回食物；时装礼盒等物品能出现在映射表里，但回家后不会进衣柜。
如果切换了自定义 `WorldPack`（如 `--pack indie-studio-3`），可用 `ai-house init --pack indie-studio-3` 自动在 `.env` 生成 `BOTGAME_HOUSE_ROSTER_JSON`，城市引擎启动时会按该名单动态注册居民。

### 模式 B：公共城市远足（`city_mcp_outing`）

无需本地城市容器，把任意公开运行的 bot-game-mcp 晴空都地址填进 `.env`，**每位室友使用各自的城市令牌**：

```ini
CITY_FEDERATION_MCP_URL=https://city.example.com/mcp
CITY_FEDERATION_TOKEN_NELL=<奈尔自己的令牌>
CITY_FEDERATION_TOKEN_YUKI=<由纪自己的令牌>
CITY_FEDERATION_HOUSE_TAG=樱丘小屋
```

只有一把令牌时，用 `CITY_FEDERATION_TOKEN` 并用 `CITY_FEDERATION_ACTORS` 指明**恰好一位**室友；一把令牌不能给两位室友用，否则远端会把她们当成同一个人。每位室友有独立的 MCP 会话，进城时不抢占别的客户端的控制权（`claim=false`）。

> **真实内核门禁已接通（公共城内互动仍为实验性）**：`Outings.guard` 会询问 `CityMcpOutingExtension.is_door_move`，**仅放行该插件当前有效行程标签（`local_tag` / `home_tag`）发起的离家与回家移动**；未登记的普通 `move(to='away')` 依然会被内核以 `outing_plan_required` 拦下。出发前 `_departure_gate` 先做只读预检，确保内核不允许外出时不向公共城市发任何请求。

补充说明：①上游的 `leave_world` 一进来就丢掉这个 MCP 会话的城市上下文，不管成功、报错还是应答丢失，所以之后每一次核对都改用 `enter_world(claim=false)`（它也会返回 `control.mine`）；城市回 `NOT_IN_CITY`（会话上下文丢了）时同样改为重新核对。这意味着离城应答丢了之后，核对可能把居民重新放回城里再离开，所以「只发一次 `leave_world`」并不成立，成立的是「控制权不是我们的就一次都不发」。②核对和离城是两次调用，两次之间控制权仍可能被别的客户端接走，这是上游没有原子操作造成的残余风险。③只有室友真的出门（内核受理了本次行程提交的那一步移动）后，生活层才会暂停她的日常；没出成门的残留记录不会让她停摆，只会挡住下一次出发，直到核对清掉它。内核受理后又把这一步判为失败或取消、而她仍在家时，记录会被当作没出成门的残留，按谨慎的方式离城清掉，不写「回来了」的记忆。④返程：内核受理「回家」这一步不等于到家，记录要等内核回报这一步成功（或她已不在外面）才清掉、才写「回来了」的记忆；失败或消失则重新提交。⑤尚未覆盖：回程在真内核门禁下的放行（现在出门本身就被门禁拒，所以还轮不到它）、城市桥的 `home_allowed` 预检。

这个插件现在只做：进城 → 乘车到目的地 → 定期看同地点有谁（读 `here.people`）→ 到点离开 → 内核确认她到家后，把「遇到了谁」写入本地受众隔离材料库（`materials`）。**城里答应了、本地内核也接受了移动（回执为 accepted/running/succeeded），室友才会在本地变成「外出」**；城里明确拒绝（工具返回错误）则室友留在家里；本地内核在出发后才拒绝（例如她刚好忙起来）则先离城再报错。

**行程记录与实验动作**：每次出发前先把记录写进本地库（重启不丢），每一步成功再更新；记录里存着本次出门那一步的标签，崩溃恢复时只认内核里**这一步**的回执（她正在做别的事、或人在别处都不算）。上游的 `leave_world` 不核对控制权，所以本插件**只在刚读到 `control.mine` 为真时才发 `leave_world`**：控制权在别的客户端手里或读不出来时，什么都不发，记录保留并标 `needs_check`（`check_reason` 写原因），不会盲目补偿，也不会重复出发。进城、返家这类远端调用若超时、结果未知，同样只留记录、不补偿、不重发；之后每分钟用 `enter_world(claim=false)`（新会话）或 `get_my_view`（旧会话）核对一次——是我们的就离城并清记录；**不是我们的（控制权转给了别的客户端）则保留记录并标 `control_lost`，自动处理停止，等房主决定，绝不自动删记录**。注意：核对用的 `enter_world` 会让已掉线的居民重新在城里。离城失败时外出状态保留并每分钟重试，最多 5 次；控制权丢失（`control_lost`）或重试用尽（`stuck`）后自动处理停止，需要房主用 `POST /api/city/return`（`{"actor_id": "...", "force": true}`）：只有确认居民是我们的才会发 `leave_world`，不是我们的就只让她回家；读不出来时拒绝并保留记录，除非再加 `"abandon_remote": true`（会写一条放弃记录；对「还没出门」的记录同理）。接口返回的 `home` 为真才表示内核确认她到家了，`returned` 只表示这次返家请求已处理。

本版额外在 `CityMcpOutingExtension` 中提供四个**实验性**公共城市接口：`say`、`drift_book`、`take_book`、`world_wait`（调用前同样强制校验 `control.mine`）；把进城礼盒带回本地衣柜等功能暂未提供。

**传出去的资料是公开的**：进城时发给城市的显示名（室友名字加小屋标签，最多 16 字）和简介（取自室友资料，最多 80 字）会被城里所有玩家看到，请不要在室友的简介里放不想公开的内容。首次进城的启动资金由城市自己的配置决定（上游默认值可改），不是每位室友都会领到。房主的私宅数据（聊天、日记、记忆）全程保留在本机，不会发给城市。

**核对时不发资料、`home` 的含义**：上游在判断控制权之前就会先更新已有居民的简介，所以重启后的核对（`enter_world` 的 `claim=false` 探测）**只带 `client_id` 和 `claim`，不带 name / bio / entry**，不会覆盖别的客户端已经发布的简介；只有出发那一次进城才带资料（那是这位室友自己的进城）。探测在居民根本不存在时可能被城市拒绝（第一次进城需要名字），这种情况记录会保留、等房主处理，不会自动放弃。`POST /api/city/return` 的返回里，`home` 为真要同时满足「没有未结行程记录」和「内核说她不在 `away`」；没有联邦记录不等于她到家了（她可能在按普通计划外出），这时 `home` 为假、`returned` 为假；`trip_open` 表示还有行程记录没结。
