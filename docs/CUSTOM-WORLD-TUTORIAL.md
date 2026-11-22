# 自定义世界包（WorldPack）与插件扩展教程

无需修改内核代码，只要新增一组 JSON 文件即可替换屋子成员、性格设定、房间列表与食谱。仓库已自带 `packs/indie-studio-3`（3 人独立游戏工作室示例包，并作为安装包内置资源随分发包一起提供）。

## 1. 一键试跑内置的 3 人工作室包

```sh
ai-house pack-check indie-studio-3
ai-house init --pack indie-studio-3 --force
ai-house doctor
ai-house up
```

当传入 `--pack indie-studio-3` 时，`ai-house init` 会同时把 `BOTGAME_HOUSE_ROSTER_JSON` 写入 `.env`，以便你启用 `docker compose --profile city` 时让本地「晴空都」城市引擎自动识别新的住客 ID、昵称与简介。

## 2. 目录结构（WorldPack）

参考 `packs/indie-studio-3/`：

```text
packs/my-custom-world/
├── manifest.json       # 世界名称、profile、成员列表与可选房间覆盖
└── people/
    ├── <actor_a>.json  # 姓名、bio、persona、做饭优先级、夜猫子系数、衣柜主题色
    └── <actor_b>.json
```

- `manifest.json`：至少声明 `vocab_version`、`profile`、`people`（有序 ID 列表）。未覆盖的食谱、服装、活动词表会自动从默认模板继承，你也可以按需放置同名 `recipes.json` / `ingredients.json` / `snacks.json` / `activities.json` / `places.json` 覆盖。
- `people/<id>.json`：包含 `name`、`bio`、`persona`、`accent`（十六进制颜色）、`cook_priority`、`night_owl`。
- 检验命令：`ai-house pack-check ./packs/my-custom-world`（返回 0 且列出 `roster`、`names`、`cook_order`、`recipes` 即合格）。

## 3. 四轴插件与工具开发（Plugin SDK）

`src/ai_house/plugin_api.py` 定义了五类扩展入口（均可通过 `pyproject.toml` `[project.entry-points]` 注册，或使用 `--packs-root` 自动发现）：

| 入口组 | 基类 / 形式 | 用途 | 内置示例 |
| --- | --- | --- | --- |
| `ai_house.packs` | `WorldPackSpec` | 新增整套世界与角色包 | `bundled_packs/indie-studio-3` |
| `ai_house.extensions` | `LifeExtension` | 插入生活规则、外部联动或 `is_door_move` 门禁 | `book_nook.py`, `city_mcp_outing.py` |
| `ai_house.channels` | `ChannelPlugin` | 接入外部群/私聊传输通道 | `onebot_ws.py` |
| `ai_house.providers` | `StudioProvider` | 响应衣柜渲染或日志外部订阅 | `svg_wardrobe_studio.py` |
| 对话工具 (`AI_HOUSE_TOOLS`) | `{spec, fn}` | 供室友大模型在聊天时按需调用的工具 | `sticker_pack.py`, `dice_tabletop.py`, `book_nook.py` |

最小 `LifeExtension` 骨架（可参考 `src/ai_house/builtin_plugins/book_nook.py`）：

```python
from ai_house.plugin_api import LifeExtension

class TeaBellExtension(LifeExtension):
    shops = False

    def step(self) -> None:
        pass  # 每个内核 tick 调一次；写状态请使用 `with self.life.s.tx(): self.life.put(...)`

    def context(self, actor_id: str, decision_kind: str):
        return {"tea_bell": "玄关放着刚沏好的焙茶"}
```

通过环境变量 `AI_HOUSE_EXTENSIONS=pkg.mod:TeaBellExtension` 即可在启动 `ai-house up` 时挂载。
