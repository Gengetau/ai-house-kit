# 这个目录里的文件从哪来

`memory.py`、`qq_identity.py`、`intents.py`（约定：意图与承诺）是私有部署里的同名文件，搬过来时只做了两类处理：

1. **去私有化**（机械替换，不改逻辑）：真实号码换成同长度的假号，住客与房主的名字、昵称换成占位名，内部路径换成中性写法。
2. **下面这张表里的改动**。除此之外逐行相同。`tools/drift_report.py` 可以随时量两边差了多少行（2026-11-25，私有端的行数 / 其中逐行相同的：`memory.py` 1957 / 1802，公共文件 2524 行，多出来的主要是并入的可信度一段、没有向量时的关键词一段和含糊问句的兜底；`intents.py` 1161 / 1155；`qq_identity.py` 241 / 238）。

`world_shim.py` 也是 kit 自己写的，见下面的专门一节。`roster.py`、`loader.py`、`__init__.py` 是 kit 自己写的：私有部署的花名册是一张手写的表，kit 的住客来自人物包，所以这里是空表加一个造表的函数。

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
| `memory.py` | **行为**：新增信任等级（`MEMORY_TRUST=1` 才生效，默认关）：`mem.trust`（verified / group_stated / hearsay）、`add(trust=)`、检索按等级降权、与已核实记忆矛盾的未核实说法被压低并标注、召回行写明“群聊陈述·未核实”等、合并时弱的不覆盖强的、抽取器按谁说的定等级。关着时不加列、输出与原来逐字相同 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。抽取器怎么定等级、记不记，见下面各行（同一天并入，同样只在 `MEMORY_TRUST=1` 时运行）；他人对 verified 记忆的改写另存一条 |
| `memory.py` | **行为**：`add(expires_at=)`（一个时间，`None` = 永不过期，与原来一致）、`mem.expires_at` 列、`_sweep()`：过期的记忆在每次读写开头被标成 `status='expired'`，之后搜索、召回、名片、规矩、日记材料、泄露检查、改与忘都找不到它 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。没有默认寿命；没有过期行时每次读只多一次走索引的空查询。`ttl_s` 由适配层换算成 `expires_at` |
| `memory.py` | **行为**：`search()` 在查询没有向量、或某条记忆没有向量时，除原来的字符对规则外，再按“问句里的实义词”算一次（去掉「你还记得我…吗」这类套话和被问者的名字后，记忆文字含有其中 60% 以上的权重且至少整词命中一个），取两者中较高的分；新增 `_lexical_features`、`_distill_query`、`_coverage`、`_novec_score` 和 `_QUERY_NOISE_RE` 等几个常量，从 kit 原来的记忆库搬来。**查询和记忆都有向量时这段不运行，分数与结果和原来逐位相同**（`world-kernel/test_m1_common_novector.py` 钉了一组分数） | 维护者 2026-11-24 定。没有向量服务时，「你记得我对什么过敏吗」找不到「房主对花生过敏」：套话的字符对盖过了唯一的实义词。原规则保留作下限，只会多找到，不会少找或改变排序依据 |
| `memory.py` | **行为**：`recent_block()` 拿不到向量时（没有服务、挂了、忙），若 `MEMORY_RECENT_KEYWORD=1`，按同样的关键词分数挑未整理的行：只比说话的内容（不比发言人的名片，问句里被问者的名字也先去掉），她自己说的话、整句都是提问的话、不到 4 个字的话不算“被告知的事”，不挑（新增 `_only_asks`）；其余（谁能看哪些行、时间窗、条数、影子开关、措辞）不变。开关默认 0：拿不到向量就什么都不带，和原来一样 | 维护者 2026-11-24 定。默认关是因为私有端的用例（`test_memory_recent.py`）把“向量失败就不带”钉成了要求；没有向量服务的小屋把它打开。有向量时开关无影响 |
| `memory.py` | **行为**：新增 `backfill_vectors(batch=32)`：给没有向量的有效记忆补向量，一次 `batch` 条，直到补完或服务不答；返回补了几条；没有向量服务时什么都不做，返回 0。别处不调用 | 维护者 2026-11-24 定。后开向量服务的小屋，旧记忆没有向量（15.7 的 N9）。私有端不调用它，行为不变 |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，抽取器认得转述句（`_RELAY_RE`、`_is_relayed_line`：「小林说他对花生过敏」「听说…」；说的是说话人自己的事，如「医生说我对花生过敏」，不算转述）。房主转述别人的话，记成 claim / group_stated，`source` 写 `auto:relayed`；抽取器自己从不标 hearsay。关着时不运行，输出不变 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入（`_is_relayed_line`）。没有它，房主复述群友的话会被记成房主自己的说法 |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，记忆的文字要和房主原话对得上才算 verified。群里：房主（按号码）在 op 的 `src` 点名的、不是转述也不是纯提问的行里说过，文字和那几行有实词重合，且文字里没有别人的话里才有的二字组（`_TRUST_IGNORE_BG` 里的词不算），才是 verified，并把 `source` 写成 `auto:owner_verified`；否则 claim / group_stated，群规矩（rule）直接拒绝。房主私聊里：`_owner_lines_support` 逐项核对被引用的原话（改了的月份、过敏原、生日、店名、否定词都算没说过；「私下」「悄悄」「要求保密」这类说话方式、记录当天的日期、括号注释不算内容）。关着时不运行，输出不变 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入（`_resolve_evidence_and_trust`、`_owner_lines_support`） |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，房主私聊里“房主原话撑不住”的记忆是推断：kind 落到 claim（fact / preference / person / claim 四类；event、promise 等保持原 kind），等级 group_stated，召回写「推断·未核实」，仍被召回。房主原话撑得住的 claim，若说的是房主自己、引用的行都是第一人称且不是转述，改成 fact / verified。关着时不运行，输出不变 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入 |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，`add()` 认 kind `claim`（别人的一面之词）：未指定等级时是 group_stated，召回行按未核实写（群里「群聊陈述·未核实」，房主私聊里「推断·未核实」）。关着时 `claim` 仍落成 `fact`，等级列不存在，措辞不变 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入（kit 的 `claim` 类别）。抽取提示词不教模型写 claim：由上面两行的规则判定 |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，抽取的证据门槛：op 的 `src` 点名的行里要有一行不是她自己说的（没写 `src` 就看这一批里所有不是她说的行），否则拒绝（算 rejected）；群里还要有一行不是纯提问、且和文字有实词重合，她自己的猜测里才有的新东西（二字组）不能写进文字。群里 op 不写 `src` 就不会是 verified。抽取提示词里的聊天行加上「L数字」行号，并加一条说明（`TRUST_PROMPT`，即 `EVIDENCE_PROMPT` 的第一条；已开 `MEMORY_EVIDENCE` 时用它自己的）。关着时不运行，提示词不变 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入。行号不带 `〔即…〕` 日期（那是证据层的事） |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，别人对 verified 记忆的改写（update）另存一条 claim / group_stated（原来是沿用旧 kind），`source` 同样记 `auto:relayed` 或 `auto`。关着时不运行 | 维护者 2026-11-24 定，从 kit 原来的记忆库并入 |
| `memory.py` | **行为**：`MEMORY_TRUST=1` 时，等级的降权只改排序，不抬高召回门槛（`search()` 里按 `min_score * 权重` 比较）：没有向量时一条“大致对得上”的分数只有 0.5 上下，乘 0.78 后落到门槛下，被降级的记忆就永远想不起来。关着时权重是 1，比较与原来逐位相同 | 维护者 2026-11-24 定。kit 原来的记忆库分数尺度不同，门槛在降权之后比；搬过来要换成这个比法 |
| `memory.py` | **行为**：从房主私聊里（工具 `update`/`forget`，和抽取器整理私聊时的 `update`/`forget`）：公共（`public`）记忆一条也改不了、忘不了；她自己的记忆哪一类都还能忘；只有她的私密（`private`）记忆能改写，改别的范围的会被拒绝（抽取器里算 rejected）。`_own_row()` 新增参数 `rewrite`。群里能改能忘的不变 | 维护者 2026-11-24 定（15.7 的 N1），和 kit 原来的记忆库一致。原来房主私聊里的话可以被写进群里读得到的记忆。**私有端换用后：私聊里整理记忆时，对群里记忆或公共记忆的 `update` 会被拒绝，原来是改写了** |
| `memory.py` | **行为**：`recall()` 里问的是“你记得吗”这类问题（`_asks_if_remembered`），又什么都没召回（没有命中、也没有刚说的话）时，若问句没有实义词，或（在群里）问句自己说了是私下说的（`_PRIVATE_TALK_REF_RE`：私下、悄悄、告诉过你…），就给她最近 3 条她看得到的私密记忆（`_visible_sql` 里 `scope='private'`、非日记、非密钥样），群里再加最近 3 条还没整理的房主私聊原话（`recent_block(latest=True)`，只取私聊、不是她自己说的、不是纯提问、不到 4 字的不算；仍受 `MEMORY_RECENT` / 时间窗 / 影子开关管）；房主私聊里只有问句没有实义词时才兜底。点了具体话题又查不到的，仍是 `NO_MEMORY_NOTE`。开关 `MEMORY_VAGUE_FALLBACK`，默认 1。有没有向量都一样；有向量时分数不变 | 维护者 2026-11-24 定（N5），从 kit 原来的记忆库并入（`_recent_private`、`_pending_private(latest=True)`、`_PRIVATE_TALK_REF_RE`，d179278）。「我之前悄悄告诉你的那件事，你还记得吗」原来答“不记得”。**默认开，私有端换用后行为就是这样**；要保持原样设 `MEMORY_VAGUE_FALLBACK=0` |
| `memory.py` | **行为**：群里召回末尾那句“标着·私密的是…”（`_private_note`）只在这次带出的内容里真有私密记忆（`scope='private'`）或私密的刚说原话时才加；原来只要开了“私聊记忆进群”、召回有内容就加。无开关 | 维护者 2026-11-24 定（N10），和 kit 原来的记忆库一致。**私有端换用后：群里召回没带出私密内容时不再有这句**（措辞上的变化） |
| `memory.py` | `db()` 返回的连接在 `with db() as c:` 结束时除了提交（或回滚）还会关闭（`_Conn`）。原来 sqlite3 自己的 `with` 只提交不关，每次调用开一个连接，都留给垃圾回收去关 | 不改行为。Python 3.13 会为每个没关的连接报 `ResourceWarning`；长跑的进程里连接也不该靠垃圾回收。**私有端换用后无可见变化**（41 处 `with db()` 都只在块内用连接，逐一查过；私有端 113 个用例照过） |
| `memory.py` | **行为**：`recent_block()` 给“房主私聊里刚说、还没整理成记忆”的那几行写的标签，在免查的群里改成「房主私聊·私密：这里是小屋自己的群，想提可以自然地提」；别处仍是「…你知道，但别在群里说出来」 | 维护者 2026-11-24 的口径（免查的群里可以自然地提）。原来这几行在哪个群都写“别说出来”，而同一个群里已整理的私密记忆（`_private_note`）却说可以提，两句话互相矛盾。私有端换用后免查群里这几行的标签会变 |
| `memory.py` | **行为**：`leak()` 比字符对时，把回复再读一遍：两个中日韩文字之间夹着的空格、标点、下划线去掉后再比（`_UNSPACE_RE`）。原来「冬 至 要 在…」「冬.至.要.在…」这样隔开写的复述一对字符都对不上，两条规则都放行 | 缺陷修正（kit 原有的测试查出来的）。拉丁字母的词不拼接（拼起来几乎和什么都有重合）。只会多拦，不会少拦；私有端换用后同样生效 |
| `qq_identity.py` | `import roster` 同上处理 | 同上 |
| `qq_identity.py` | 房主的别名不再写在这个文件里，改为读花名册 `OWNER["aliases"]`（没有就只认「房主」「owner」） | 别名是数据，不是逻辑 |
| `intents.py` | 开头两行 `import world as W` / `import qq_identity as ID` 改成：在包里 `from . import world_shim as W, qq_identity as ID`，否则照旧 | 同一个文件两边都能用；私有端的 `world` 是它自己那个大模块，kit 里同名的那几样在 `world_shim.py` |
| `intents.py` | **行为**：新增开关 `INTENTS_PRESENCE`（模块导入时读，和 `INTENTS_SHOW` 一样，默认 `1`；模块里的变量叫 `PRESENCE_ON`，因为 `PRESENCE` 这个名字原文件已经用作建表语句）。关着时 `queue_world` 不再调用 `presence_from_chat`，`presence_from_chat` 自己也直接返回 `None`：不从聊天里推房主在哪个房间，也不往房主化身的队列里写东西；`owner_chat_queue` 照常写 | 维护者 2026-11-24 定（「从聊天里判断房主在哪」做成开关）。**默认开，私有端换用后行为不变**；kit 的守护进程以后自己决定开不开 |
| `intents.py` | **行为**：`_structured` 里把约定编成内核条件的那一行多传一个参数：`rules.compile_terms(d["text"], vocab, d["party"], d["beneficiary"])`（原来没有最后一个）。`promise_rules.compile_terms` 同时加了可选参数 `beneficiary`，只在受益人是房主时，才把「替／帮／给＋某个短名字＋买／带」里的名字当成房主的昵称剥掉 | 2026-11-25 真机实测：QQ 私聊里模型写的约定摘要用的是房主的 QQ 昵称（「替某某买一盒红茶带回来」），原来的语法不认识这个名字，约定登记了却不会被执行。**两边要一起换**：只换 `intents.py`、内核的 `promise_rules.py` 还是旧的，这一行会报参数个数不对 |
| `intents.py` | **行为**：`_bound_yes`（判断住客有没有对提议明确说“好”）三处放宽，其余的否定检查（`_unreal`、`NO`、`NO_EVIDENCE`、必须紧跟提议那一行、引用了提议的分支）原样不动：① 回复开头的问候（早上好、早安、你好、单独的“早”等，后面可跟标点或～）先剥掉再找“好”；② 第一句才是回答：它不能带问号，且要以 `YES_START` 开头，它的尾巴照旧过原来的尾巴检查；第一句的尾巴不过时，退回原来的做法，检查整段回复的尾巴；③ 后面的问句只有在和提议同话题（`_same_topic`），或带“确定／真的／当真／非要／一定要／还是算了／要不算了”时才否决，问别的事（比如“要不要顺便给你留一份吐司？”）不再算，这类句子也不再被 `NO` 误伤（“要不要”里含“不要”）。模块级新增 `GREETING`、`DOUBT`、`SENTENCE` 三个正则。只会让原来判“不是好”的少数回复变成“是好”，原来判“是好”的一概不变 | 2026-11-25 真机实测两次：房主说“奈尔，早上好。今天出门的时候帮我带一盒红茶回来，好吗？”，她回“早上好~好的,红茶我记下了…”（好在问候之后）和“好呀，…我正在厨房做法式吐司，要不要顺便给你留一份？”（后面一句无关的问句），都被判成没说好，约定因此没登记。**两边要一起换**，否则同一句话在两边判得不一样 |
| `intents.py` | **行为**：`_end` 里，只有 `intent_drop`（取消／拒绝）接受“受益人”（列 `beneficiary`，空或 NULL 当空）说的话作证据，原来只认当事人（`party`）；`intent_done`（做完了）仍然只认当事人，其余检查（受众、私聊不能在群里结束、问句、`_unreal`、`ASKING`）不变，事件的 `by` 仍是说话的人 | 2026-11-25 真机实测：她答应房主帮他带可乐，房主说“后天的可乐不用买了，我自己带了。”，抽取模型的 `intent_drop` 引了房主这句，被 `_end` 以“没有当事人的证据”拒绝，约定一直开着，她还会去买（`_new` 本来就认当事人加受益人，`_end` 不认）。**两边要一起换** |
| `intents.py` | 除上面五条外逐行相同。注意 `_rules()`（找 `promise_rules` 和词表）按私有部署的目录去找旁边的 `world-kernel`，在 kit 的包里找不到这个目录：用的时候要么 `worldkernel` 已在 `sys.path` 上，要么适配层把 `_rules` 换成自己的（带上按包绑定的 `promise_rules.bound(vocab)`）。这里没改 | 不改共享文件；换法留给适配层 |

## `world_shim.py`：私有部署的 `world` 里，约定用到的那几样

`intents.py` 在私有部署里 `import world as W`，那是一个几千行、负责住客一天生活的模块，kit 用自己的内核，不带它。约定只用到其中很少几样：开库（`db`）、谁是谁（`actor_of`、`_key`、`ACTORS`、`CONSUMER_ACTOR`）、日界和时区（`jst`、`_day_start`、`JST`）、清洗与隐私判断（`_clean`、`_private`）、事件日志（`_event`）、住客互相联系那个功能的两个帮手（`_together`、`_add_contact`），再加几个常量和钩子（`DB_PATH`、`HOME_CHAT`、`DM_AUDIENCE`、`INTENTS`）。函数的内容取自私有文件里的同名函数，和它们的差别只有：

| 改动 | 原因 |
|---|---|
| `db()` 不设日志模式（私有端设了）；`with db() as c:` 结束时除提交或回滚外还关闭连接（和 `memory.py` 的 `_Conn` 一样） | kit 的 `world.db` 的日志模式归内核定，约定不去改；连接不靠垃圾回收去关 |
| 没有默认的“小屋群”：`HOME_CHAT` 默认为空，由调用方设（`group:<n>`） | 私有端写死一个群号；kit 的小屋可以有几个群，也可以没有 |
| `_together` 没有房间表（`LOCATIONS` 为空），所以永远算“不在一起”；`_add_contact` 照旧写 `contacts` 表。**`nudge`（到期了却不在一起时去联系她）在 kit 里不能用**，没有接线 | kit 没有“住客互相联系”这个功能，也没有私有端那张房间表 |
| `init()` 只建约定要写的两张表（`events`、`contacts`），私有端 `world` 的其余表不在这里 | 其余的属于 kit 没有的功能 |

和 `memory.py` 一样，每个小屋一份：用 `loader.load()` 载入一份副本（现在同时载入 `qq_identity`、`world_shim`、`memory`、`intents` 四个，用 `loader.part(memory, "intents")` 取），再设 `DB_PATH`、`HOME_CHAT`。`loader.load()` 不会替你打开 `memory.INTENTS`。

## `promise_rules.py`（world-kernel）的改动

kit 里的 `worldkernel/promise_rules.py` 原来把“谁能许诺”（`BOTS`）和要剥掉的名字写死成默认那六个人，换成别的包（比如 `indie-studio-3`）就永远判不出完成。新增 `bound(vocab)`：返回一个带 `compile_terms` / `judge` 的对象，人物和名字取自传入的词表。模块级的 `compile_terms` / `judge` 行为不变。这不是私有端的文件，登记在这里是因为 `intents.py` 靠它判完成。

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

## 约定的测试

`intents-conformance-tests/`：私有部署 4 个约定测试文件共 75 个用例，加上私有端 `test_world.py` 里它们要用的那一小段（`WorldBase`、`at`、`answer`）。71 个通过，4 个跳过：
`ShownTests` 里的 `test_card_shows_due_soon_only_when_on`、`test_world_scene_with_both_completes_it`、`test_world_alone_cannot_complete_a_joint_one`、`test_due_and_apart_reaches_her_only_when_free` 要私有部署的世界引擎（`card`、`advance`、`llm`、`outreach`），kit 没有；该目录的 `world.py` 给这四个名字的是会抛 `SkipTest` 的占位，跳过信息写明原因。别的用例都没有跳过，也没有标成预期失败。

- **改了一处**：`test_intents_kernel191.py` 里 `test_qingkong_park_requires_arrival_and_actual_joint_activity` 的地名换成了 kit 目录里同一个公园的名字（“南琉璃公园”，`minami_park`）。这是去私有化留下的差别，其余文件与去私有化后的原件逐字相同。
- 私有端换用这里的文件时，按它自己的目录写这个地名。

## 约定

- 这里的行为改动要和私有端一起做；只在一边改，就是在重新制造漂移。
- kit 需要而私有端没有的东西（时钟注入、每个小屋各自的库、模型与向量的接法）放在适配层，不放进这两个文件。
- 维护者同意并入公共代码的 kit 行为（见 `docs/PORTING-PLAN.md` 14.5）并入时在上表登记。
