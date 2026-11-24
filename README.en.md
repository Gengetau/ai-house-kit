# ai-house-kit

**Bring an AI sharehouse to life in one command — roommates with daily routines, cooking & outfits, audience-isolated long-term memory, personal evening diaries, and optional Qingkong City trips.**

> 📖 **[中文 README](README.md)** · 🧩 **[Custom WorldPack & Plugin Tutorial](docs/CUSTOM-WORLD-TUTORIAL.md)** · 🏙️ **[Plugins & Qingkong City](docs/PLUGINS-AND-CITY.md)** · 💬 **[Chat & QQ Setup](docs/CHAT-AND-QQ.md)** · 🚀 **[Always-on & side-by-side deployment](docs/DEPLOY.md)**

## Highlights

- **Zero-dependency turnkey runtime & First-Bot Onboarding (Linux / macOS / Windows)**: Built purely on Python 3.11+ stdlib + SQLite with zero frontend build step. Fresh installs launch directly into the `#/setup` **First-Bot Onboarding Wizard** where you create your first AI roommate, configure & live-test any OpenAI-compatible key (`AI_HOUSE_API_KEY`, plus optional `AI_HOUSE_FALLBACK_*` failover) right in the browser, or start in zero-key demo mode—with bundled 3-person (`indie-studio-3`) and 6-person (`life1`) sample packs one click away.
- **Warm Lamp Pixel Studio**: Features a 2-layer cutaway house map, 11 isometric rooms with owner avatar walk-in, pixel character sprites, wardrobe browser, flipbook evening diaries, audience-isolated memory search/forget, kitchen stock restocking, live roommate invite/edit, in-browser LLM hot-reload, and a standalone `/chat` view.
- **~98% lower LLM cost (~100 calls/day for 6 roommates)**: Sleeping, hygiene, and everyday routines run on deterministic L0 schedules. Models run only at real decisions and chat turns, with lock-free HTTP execution so slow model calls never freeze the studio UI.
- **Chat promises that actually get carried out**: Group or private owner agreements write to `house_intents`, trigger errand shopping via `Shopping`, and reconcile against kernel receipts (`intent_evidence` + `script`) to mark `completed`.
- **Audience-isolated long-term memory & evening diaries**: `MemoryStore` isolates `private` DMs from `group` / `world` recall by default (a maintainer can open chosen groups with `AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS` / `AI_HOUSE_MEMORY_LEAK_FREE_GROUPS`, see `docs/CHAT-AND-QQ.md`) and runs outbound leak checks, with one-click forgetting in the Web Studio. At `22:30 JST`, `DiaryService` writes first-person personal diaries purely from daytime facts (`daylog`/`script`), never touching private DMs.
- **4-axis plugins & WorldPacks**: Customize characters, rooms, recipes, and outfits via JSON packs (`ai-house pack-check indie-studio-3` ships bundled). Includes 3 built-in conversational tools (`sticker_pack`, `dice_tabletop`, `book_nook`).
- **Dual-mode Qingkong City linkage ([bot-game-mcp](https://github.com/Gengetau/bot-game-mcp))**:
  - **Mode A (local private city via Docker Compose)**: `docker compose --profile city up -d --build` runs `qingkong-city` on an internal network and syncs custom WorldPack rosters via `BOTGAME_HOUSE_ROSTER_JSON`.
  - **Mode B (public city MCP, experimental)**: Enforces the real kernel outing gate (`Outings.guard -> is_door_move`) for departure/encounters/return, plus experimental public actions (`say`, `drift_book`, `take_book`, `world_wait`). Bringing wardrobe gifts home from the city is not yet supported.

## Quick Start (Local Python)

```sh
git clone https://github.com/Gengetau/ai-house-kit.git
cd ai-house-kit
python3 -m venv .venv && . .venv/bin/activate   # Windows: py -3 -m venv .venv, then .venv\Scripts\activate
python3 -m pip install -e .          # add ".[onebot]" for QQ, ".[dev]" to run the tests, ".[agent]" for agent-backed residents (docs/AGENT-BRAIN.md)
ai-house init && ai-house doctor
ai-house up
```

Open **`http://127.0.0.1:8780`** (using your `AI_HOUSE_OWNER_TOKEN` from `.env`, or `/chat` for the chat-only view):

- Fresh launches without `--pack` enter `#/setup` to create your 1st custom AI roommate and optionally test/save your LLM API key (you can invite more roommates, edit profiles, or reset the house later from `#/ops`).
- Or pass a bundled sample world directly: `ai-house init --pack indie-studio-3 --force && ai-house up`
- Loopback (`127.0.0.1`) can run without a token if none is configured; binding `0.0.0.0` or Docker requires a >=16-char `AI_HOUSE_OWNER_TOKEN` (generated automatically by `ai-house init`). Always send tokens via `Authorization: Bearer <token>`, never query parameters (`owner` enables setup/admin/reset alongside `read` / `write` / `private` tiers).

## Docker Compose (Single & City Profiles)

```sh
docker info                          # make sure the Docker daemon is running first
python3 -m pip install -e .          # only needed for `ai-house init`; use the virtual environment from above
ai-house init
docker compose up -d --build
# Or include the local Qingkong City container (set WORLD_KERNEL_CITY_BRIDGE=1 in .env):
docker compose --profile city up -d --build
```

## Run the Full Test Suite

```sh
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-kernel -p "test_*.py"
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-catalog -p "test_*.py"
PYTHONPATH=src:world-kernel:world-catalog python3 -m unittest discover -s world-contract-tests -p "test_*.py"
```

## License

- Code: [Apache License 2.0](LICENSE)
- Sample world data (`world-catalog/worldcatalog/life1/`, `packs/`, `src/ai_house/bundled_packs/`, `world-kernel/recordings/`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- Third-party notices: [NOTICE](NOTICE)
