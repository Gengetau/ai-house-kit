"""Reusable 32-virtual-hour Stage 2 live-model regression runner (requires AI_HOUSE_API_KEY)."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.cli import _load_dotenv  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402


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
            d.converse("group", "苍空今天帮我带 2 个洋葱回来，另外记住我对虾仁过敏", owner_speaker("房主"), actor="sora")
            for _ in range(32):
                d.fast_forward(60)
            usage = d.get_usage_summary(period="all")
            print(f"[stage2-32h-live] 完成 32 虚拟小时推进：calls={usage['totals']['calls']} tokens={usage['totals']['total_tokens']}")
        finally:
            d.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
