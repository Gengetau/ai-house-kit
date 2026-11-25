# intents-conformance-tests

私有部署自己的「约定」（意图与承诺）测试，对着 kit 里的公共模块（`src/ai_house/common/intents.py`）跑。
它们通过，说明这份公共代码的行为与私有部署一致。kit 把这份代码接进小屋的那一层（`src/ai_house/intents_common.py`）
由 `world-kernel/test_m2_intents_adapter.py` 等测。`test_presence_switch.py` 是 kit 自己加的：测 kit 在公共文件里加的那个开关。

```bash
cd intents-conformance-tests
PYTHONPATH=../src python3 -m unittest discover -s . -p 'test_*.py'
```

- `test_intents.py`、`test_intents_evidence.py`、`test_intents_invites191.py`、`test_intents_kernel191.py`：4 个文件、75 个用例，
  71 个通过、4 个跳过（见下）。`test_intents_kernel191.py` 里 `test_seven_days_twenty_promises_two_restarts` 要跑一两分钟。
- `test_world.py`：私有部署同名测试文件里这些测试要用的那一小部分（`WorldBase`、`at`、`answer`），原样。
- `roster.py`：测试用的花名册，人物和号码都是编的。
- `_copy.py`：载入一份公共模块的副本（`loader.load`），下面几个文件都从这一份里取，所以它们共用同一张花名册。
- `intents.py`、`qq_identity.py`、`memory.py`、`world.py`：让测试里的 `import intents` 等拿到这一份副本里的同名模块。
  `world.py` 给的是 `src/ai_house/common/world_shim.py`，再加几样只有这些测试才碰的东西（都留在本目录，不进 `src/`）：
  `HOME_CHAT`、`WorldBase.setUp` 要打补丁的几个名字（`SEED_FILE`、`WEATHER`、`_seed_cache`、`_calls`、`deque`）、`init()` 里多建
  `WorldBase.set_state` 要改的 `reality` 表（用内核自己那份表结构）。

## 跳过的 4 个用例

`ShownTests.test_card_shows_due_soon_only_when_on`、`test_world_scene_with_both_completes_it`、`test_world_alone_cannot_complete_a_joint_one`、
`test_due_and_apart_reaches_her_only_when_free` 要私有部署的世界引擎（`W.card`、`W.advance`、`W.llm`、`W.outreach`），kit 没有。
`world.py` 给这四个名字的是一个会抛 `SkipTest` 的占位，所以它们显示为“跳过”，原因写在跳过信息里。**只有这四个**；别的用例都真跑。
第一个用例在跳过之前，对 `card_lines` 的断言已经跑过了。

## 与私有端不同的一处

`test_intents_kernel191.py` 里 `test_qingkong_park_requires_arrival_and_actual_joint_activity` 的地名：原来写的是私有部署目录里的公园名，
kit 的目录里同一个公园叫“南琉璃公园”（`minami_park`），所以这一个文件里把那个名字换成了 kit 的。这是去私有化留下的差别，别的字一个没动。
登记在 `src/ai_house/common/UPSTREAM.md`。

不要为了让它们通过而改这里的测试；要改，先确认私有端的同名测试也该这么改。
