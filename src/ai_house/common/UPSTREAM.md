# 这个目录里的文件从哪来

`memory.py`、`qq_identity.py` 是私有部署里的同名文件，搬过来时只做了两类处理：

1. **去私有化**（机械替换，不改逻辑）：真实号码换成同长度的假号，住客与房主的名字、昵称换成占位名，内部路径换成中性写法。
2. **下面这张表里的改动**。除此之外逐行相同。`tools/drift_report.py` 可以随时量两边差了多少行（2026-11-24：`memory.py` 1957 行里 1823 行相同；公共文件现在 2101 行，多出来的主要是并入的可信度一段）。

`roster.py`、`loader.py`、`__init__.py` 是 kit 自己写的：私有部署的花名册是一张手写的表，kit 的住客来自人物包，所以这里是空表加一个造表的函数。

## 相对私有部署的改动

| 文件 | 改动 | 原因 |
|---|---|---|
| `memory.py` | 开头两行 `import roster` / `import qq_identity` 改成：在包里用相对导入，否则照旧 | 同一个文件两边都能用 |
| `memory.py` | `MEMORY_EMBED_URL`、`CPA_BASE`、`CPA_CONFIG`、`MEMORY_MODEL` 的默认值改为空 | 原默认值是私有部署自己的地址和模型名。**私有端换用本文件时必须显式设置这四项** |
| `memory.py` | `embed()`：没设 `MEMORY_EMBED_URL` 时直接返回 None（只用关键词检索），不去发请求 | 配合上一条；原来默认值总是有的 |
| `memory.py` | `MEMORY_LEAK_FREE_CHATS` 的默认值改为空（原来写死一个群号） | 免查群由配置给出，不写死在代码里。私有端换用时要把原来那个群写进配置 |
| `memory.py` | 去掉一段只有私有部署使用的功能（约 100 行，本目录的测试都不涉及） | 不属于 kit；私有端换用本文件时需把那一段放到自己的文件里 |
| `memory.py` | 文件说明和一处注释各改了一句措辞 | 去掉部署细节和一句原话引用 |
| `memory.py` | **行为**：`leak()` 对每个没列为免查的群都查，不再要求“统一记忆”和“私聊记忆进群”都开着 | 维护者 2026-11-24 定。私聊内容进到群消息里不只有召回一条路。私有端各住客两个开关本来就都开着，实际行为不变 |
| `memory.py` | **行为**：“私聊记忆进群”不再依附“统一记忆”（`_visible_sql`、`_private_note`、`recent_block`）。没开统一记忆的住客在群里读到：这个群自己的记忆，加上她和房主私聊里的 | 维护者 2026-11-24 定。两个开关各管各的。私有端实际行为不变（理由同上） |
| `memory.py` | **行为**：密钥过滤（`SECRETS`、`DIARY_SECRETS`）多认两个关键词（`secret key`、`bridge key`）和两种不带关键词的写法（`sk-…`、`agw_…`） | 维护者 2026-11-24 定：把 kit 原来多拦的并进来。只会多拦，不会少拦 |
| `memory.py` | **行为**：日记开关关闭时，没开统一记忆的住客也能在每个会话里想起自己的日记（范围为 `own` 的那些；别人的仍看不到） | 维护者 2026-11-24 定。开了统一记忆的本来就如此（2026-11-03）。私有端各住客都是统一记忆，实际行为不变 |
| `memory.py` | **行为**：`recall()` 什么都没找到、而最后一句不是她自己说的话问的是“你记得吗”这类问题时，返回一句“照实说不记得，别编”（`NO_MEMORY_NOTE`），不再返回空。开关 `MEMORY_NO_MEMORY_NOTE`，默认开 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。空着会让模型自己猜。**默认开，私有端换用后行为就是这样**；要保持原样设 `MEMORY_NO_MEMORY_NOTE=0` |
| `memory.py` | **行为**：新增 `settings_problems()`（按模块当前的设置检查：免查的会话、房主私聊、会话别名的写法，以及各名单里的名字是否都在 `CONSUMERS` 里），`init()` 里把每条问题记成 `warning`，不拒绝启动 | 维护者 2026-11-24 定。写错的项不会报错，只会悄悄不起作用。`CONSUMERS` 为空时不查名字。只多几行日志 |
| `memory.py` | **行为**：新增信任等级（`MEMORY_TRUST=1` 才生效，默认关）：`mem.trust`（verified / group_stated / hearsay）、`add(trust=)`、检索按等级降权、与已核实记忆矛盾的未核实说法被压低并标注、召回行写明“群聊陈述·未核实”等、合并时弱的不覆盖强的、抽取器按谁说的定等级。关着时不加列、输出与原来逐字相同 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。抽取器只用简化规则（见 `world-kernel/test_m1_common_extras.py`）：房主私聊里的是 verified；群里只有每条依据的原话都是房主（按号码）说的才是 verified，否则 group_stated；他人对 verified 记忆的改写另存一条。没搬：转述识别（hearsay）、文本与原话的实词对照、私聊里“房主原话撑不住”的降级 |
| `memory.py` | **行为**：`add(expires_at=)`（一个时间，`None` = 永不过期，与原来一致）、`mem.expires_at` 列、`_sweep()`：过期的记忆在每次读写开头被标成 `status='expired'`，之后搜索、召回、名片、规矩、日记材料、泄露检查、改与忘都找不到它 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。没有默认寿命；没有过期行时每次读只多一次走索引的空查询。`ttl_s` 由适配层换算成 `expires_at` |
| `qq_identity.py` | `import roster` 同上处理 | 同上 |
| `qq_identity.py` | 房主的别名不再写在这个文件里，改为读花名册 `OWNER["aliases"]`（没有就只认「房主」「owner」） | 别名是数据，不是逻辑 |

## 一起搬来的测试

在仓库根的 `memory-conformance-tests/`：私有部署 10 个记忆测试文件里的 9 个（113 个用例），文件内容与去私有化后的原件逐行相同，只有一处例外（见下）。它们写的是 `import memory`，该目录里的 `memory.py` 把这个名字接到本目录的公共文件上，并补上上表里被清空的两个默认值。

- **没搬的一个**：`test_memory_exposure_chain.py`（6 个用例）要导入私有部署的服务入口和网桥，kit 里没有对应物，留到网桥对照那一步。
- **改了一个断言**：`test_memory_diary_acl.py` 的最后一个用例在私有端本身就是失败的——测试期望“统一记忆的住客在日记开关关闭时看不到自己的旧日记”，而代码按 2026-11-03 的决定是“看得到自己的，看不到别人的”。这里按代码的行为改了断言并在用例里注明。维护者 2026-11-24 确认：以代码为准，看得到自己的旧日记。
- **随上表的行为改动改了三个用例**（都在用例里写明了原来期望什么）：
  - `test_memory_guard.py`：原 `test_off_when_owner_dm_memories_never_reach_groups`（关掉“私聊记忆进群”后群里不查）改为 `test_a_group_is_checked_whatever_the_two_switches_say`。
  - `test_memory_recent.py`：原 `test_group_private_requires_both_permissions_and_owner` 改为 `test_group_private_requires_its_permission_and_owner`（不开统一记忆时群里仍带得出房主私聊里刚说的话，带不出别的群的）。
  - `test_memory_recent.py`：`test_pending_leak_permissions_allowlist_and_private_chat_are_unchanged` 里“统一记忆关”“私聊记忆进群关”两种情况从“不查”改为“查”。
- 这些行为改动另有 kit 自己写的用例：`world-kernel/test_m1_common_changes.py`、`world-kernel/test_m1_common_extras.py`（并入的四样）。
- **私有端换用本目录的文件时**，上面这四个用例要一起换；它自己的那份在新代码下会失败。

## 约定

- 这里的行为改动要和私有端一起做；只在一边改，就是在重新制造漂移。
- kit 需要而私有端没有的东西（时钟注入、每个小屋各自的库、模型与向量的接法）放在适配层，不放进这两个文件。
- 维护者同意并入公共代码的 kit 行为（见 `docs/PORTING-PLAN.md` 14.5）并入时在上表登记。
