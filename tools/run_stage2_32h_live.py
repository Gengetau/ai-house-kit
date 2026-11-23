"""Reusable 32-virtual-hour Stage 2 live-model regression runner (requires AI_HOUSE_API_KEY)."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.cli import _load_dotenv  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402


def verify_stage2_32h_invariants(d: HouseDaemon, *, saw_sleep: Optional[bool] = None) -> List[str]:
    """Check Trial Issues #1, #2, and #4 invariants after 32h simulation; return failure reasons."""
    failures: List[str] = []
    if saw_sleep is False:
        failures.append("问题#1失败：32 小时推进期间未观测到住客夜间睡眠")

    intents = d.intents.list_intents(actor="sora", limit=50)
    memos = [r for r in intents if r.get("kind") == "memo" and "过敏" in str(r.get("summary") or "")]
    if not memos:
        failures.append("问题#2失败：未记录房主饮食过敏 memo")
    elif any(r.get("state") != "open" for r in memos):
        failures.append(f"问题#2失败：过敏 memo 不应被关闭或过期 ({[r.get('state') for r in memos]})")

    errands = [r for r in intents if r.get("kind") == "errand" or r.get("item") == "onion"]
    if not errands:
        failures.append("问题#2失败：未创建洋葱采购 errand")
    else:
        onion_done = False
        for err in errands:
            if err.get("state") == "completed":
                ev_key = str(err.get("evidence_key") or "").strip()
                if not ev_key:
                    failures.append("问题#2失败：采购单已 completed 但缺少 evidence_key")
                else:
                    with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
                        row = wc.execute(
                            "SELECT body FROM intent_evidence WHERE key=?",
                            (ev_key,),
                        ).fetchone()
                    if row is None or ('"purchased"' not in str(row[0]) and '"brought_home"' not in str(row[0])):
                        failures.append(f"问题#2失败：采购结单凭据无效 ({ev_key})")
                    elif err.get("item") == "onion":
                        onion_done = True
        if not onion_done:
            failures.append(f"问题#2失败：洋葱采购约定在 32 小时内未自动完成 ({[e.get('state') for e in errands]})")

    for ch_id in ("qq-group:9001", "group"):
        grp_mems = d.list_memories(actor="sora", scope="group", channel=ch_id, limit=50)
        for m in grp_mems:
            if m.get("kind") == "fact" and "过敏" in str(m.get("text") or ""):
                failures.append(f"问题#4失败：群聊 ({ch_id}) 提问产生/写入了高可信过敏 fact ({m.get('text')})")
            if "虾仁" in str(m.get("text") or ""):
                failures.append(f"问题#4失败：群聊 ({ch_id}) 记忆泄露了私聊实体「虾仁」")

        grp_chat = d.list_chat(ch_id, limit=50)
        for msg in grp_chat:
            if msg.get("sender") != "owner" and msg.get("mode") != "blocked" and "虾仁" in str(msg.get("text") or ""):
                failures.append(f"问题#4失败：群聊 ({ch_id}) 回复正文泄露私聊秘密「虾仁」")

    diaries = d.diary.list_diaries(limit=20)
    for dry in diaries:
        if "虾仁" in str(dry.get("text") or ""):
            failures.append("问题#4失败：日记正文泄露私聊秘密「虾仁」")

    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run 32-virtual-hour Stage 2 regression against configured live LLM")
    ap.add_argument("--env-file", default=".env", help="Path to .env file")
    ap.add_argument("--pack", default=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"))
    ap.add_argument("--skip-if-no-key", action="store_true", help="Exit 0 with notice if AI_HOUSE_API_KEY is unset")
    args = ap.parse_args(argv)

    _load_dotenv(args.env_file)
    api_key = (os.environ.get("AI_HOUSE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        msg = "未检测到 AI_HOUSE_API_KEY / OPENAI_API_KEY，跳过联网真模型 32 小时回归。"
        print(f"[stage2-32h-live] {msg}", file=sys.stderr, flush=True)
        return 0 if args.skip_if_no_key else 2

    with tempfile.TemporaryDirectory() as tmp_dir:
        t0 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp_dir,
            pack_dir=args.pack,
            env=dict(os.environ),
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        try:
            d.converse("private:sora", "我对虾仁有点过敏，只在私聊告诉你", owner_speaker("房主"))
            d.converse("qq-group:9001", "房主有什么过敏吗？", member_speaker("群友甲", "8002"), actor="sora")
            d.converse("group", "苍空今天出门顺便帮我买 2 个洋葱回来", owner_speaker("房主"), actor="sora")
            saw_sleep = False
            for _ in range(32):
                d.fast_forward(60)
                st = d.get_state()
                if any(
                    (res.get("reality") or {}).get("availability") == "sleeping"
                    for res in (st.get("residents") or {}).values()
                ):
                    saw_sleep = True
            d.memory.extract(force=True, now=d.clock.now())
            usage = d.get_usage_summary(period="all")
            failures = verify_stage2_32h_invariants(d, saw_sleep=saw_sleep)
            if failures:
                for item in failures:
                    print(f"[stage2-32h-live] [FAIL] {item}", file=sys.stderr, flush=True)
                return 1
            print(
                f"[stage2-32h-live] 完成 32 虚拟小时推进与问题 #1/#2/#4 断言："
                f"calls={usage['totals']['calls']} ok_calls={usage['totals']['ok_calls']} tokens={usage['totals']['total_tokens']}",
                flush=True,
            )
        finally:
            d.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
