"""Drive the real Claude Code shell against `agent_mock_api.MockAnthropic` and print what happened as JSON.

Run it with a scrubbed environment (see test_agent_3_real_shell.py): it must never see real credentials.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_mock_api import MockAnthropic  # noqa: E402

from ai_house.agent import AgentRuntime, TurnRequest, prompts  # noqa: E402
from ai_house.agent.claude_engine import ClaudeCodeEngine  # noqa: E402

TOOLS = [{"name": "inspect_house", "description": "查看屋里情况", "parameters": {"type": "object", "properties": {}}}]


def main() -> None:
    mock = MockAnthropic()
    state = tempfile.mkdtemp(prefix="agent-probe-")
    env = {"AI_HOUSE_AGENT_API_KEY": "sk-ant-dummy-not-a-real-key", "AI_HOUSE_AGENT_BASE_URL": mock.base_url}
    env.update({k: v for k, v in os.environ.items() if k.startswith("AI_HOUSE_AGENT_")})
    out: dict = {"turns": [], "tool_threads": [], "permits": []}

    def start(**extra: str) -> tuple[AgentRuntime, ClaudeCodeEngine]:
        env.update(extra)
        engine = ClaudeCodeEngine(env, state)
        inner = engine.permit

        async def counted(sess, tool_name):  # the policy callback really is what admits each call
            res = await inner(sess, tool_name)
            out["permits"].append([tool_name, res[0]])
            return res

        engine.permit = counted  # type: ignore[method-assign]
        return AgentRuntime(state, env=env, engines={"claude": engine}), engine

    def runner(name, args):
        out["tool_threads"].append(threading.current_thread() is threading.main_thread())
        return {"weather": "晴", "stock": 3}

    def turn(rt: AgentRuntime, said: str, where: str = "private") -> None:
        before = len(mock.chat_requests())
        rec: dict = {"said": said, "where": where}
        t0 = time.monotonic()
        try:
            res = rt.run_turn(TurnRequest(
                engine="claude", actor="nell", channel="private:nell" if where == "private" else "group", where=where,
                system=prompts.system_prompt("你是奈尔。", where=where, allow_quiet=where == "group"),
                state_lines=["- 所在位置：客厅"], said_line="房主刚刚说：" + said,
                tool_specs=TOOLS, tool_runner=runner, allow_quiet=where == "group",
            ))
            rec.update(
                messages=res.messages, quiet=res.quiet, fresh=res.fresh_session, nudged=res.nudged, autosent=res.autosent,
                model=res.model, prompt_tokens=res.usage.prompt_tokens, context_tokens=res.usage.context_tokens,
                cost_usd=res.usage.cost_usd, tool_calls=res.tool_calls,
            )
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["seconds"] = round(time.monotonic() - t0, 1)
        rec["model_calls"] = len(mock.chat_requests()) - before
        out["turns"].append(rec)

    rt, _ = start()
    out["unavailable_reason"] = rt.unavailable_reason("claude")
    for said in ("暗号是青柠", "看看家里有什么", "发两条给我", "这次直接说", "用别的工具试试"):
        turn(rt, said)
    turn(rt, "这句不接话", "group")
    rt.close()
    rt, _ = start()  # a restarted house: the session must be resumed from disk
    turn(rt, "还记得暗号吗")
    rt.close()
    # a turn the house gives up on: the shell process is dropped from another task than the one that started it, and
    # the conversation must still be there for the next turn
    rt, _ = start(AI_HOUSE_AGENT_TURN_BUDGET_S="5")
    turn(rt, "这次等很久")
    turn(rt, "超时之后还记得暗号吗")
    rt.close()

    first = mock.chat_requests()[0]
    out["tools_offered"] = sorted(t["name"] for t in first["tools"])
    out["system_has_persona"] = "你是奈尔。" in json.dumps(first.get("system"), ensure_ascii=False)
    out["models_requested"] = sorted({str(r.get("model")) for r in mock.chat_requests()})
    out["key_sent"] = sorted({h[:14] for h in mock.auth_headers})
    out["config_dir_used"] = (Path(state) / "claude-config").is_dir() and any((Path(state) / "claude-config").iterdir())
    out["home_claude_touched"] = (Path(os.environ.get("HOME", "/nonexistent")) / ".claude").exists()
    mock.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
