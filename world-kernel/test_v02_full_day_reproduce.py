"""Flagship 24-hour end-to-end turnkey v0.2 lifecycle test.

Covers in one continuous synthetic day + restart:
1. Private owner secret DM vs. group errand promise (`house_intents` + `Shopping`/evidence -> `completed`)
2. Real kernel outing gate (`Outings.guard` -> `CityMcpOutingExtension.is_door_move`) for public city trip + encounter + return
3. 22:45 JST per-roommate personal diaries (`DiaryService` + `kind="diary"`) isolated from private DMs
4. 23:35 JST `DayNote` & `MemoryStore` extraction
5. Daemon restart on Day 2: private recall survives, group recall reads world diaries, group private-secret leak is blocked
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.city_mcp_outing import CityMcpOutingExtension, StdlibMcpCityClient
from ai_house.daemon import HouseDaemon
from test_turnkey_v021_city import FakeCity, KISSA

JST = dt.timezone(dt.timedelta(hours=9))


class FullDayTurnkeyV02Tests(unittest.TestCase):
    def test_24h_lifecycle_across_restart(self):
        city = FakeCity()
        city.place_player("tok_other", "小葵·海风寮", KISSA)
        leak_attempt = [False]

        def llm_fn(sys_prompt: str, user_prompt: str):
            if "长期记忆整理员" in sys_prompt:
                if "蓝花盆" in user_prompt:
                    return json.dumps({
                        "ops": [{
                            "op": "add",
                            "text": "2026年11月21日房主私下说备用钥匙藏在庭院蓝花盆底下",
                            "about": ["房主", "钥匙"],
                            "kind": "fact",
                            "shared": False,
                            "src": [1],
                        }]
                    }, ensure_ascii=False), 25
                return '{"ops": []}', 10
            if "睡前小日记" in sys_prompt:
                return "今天在樱丘洋馆安排了买菜和日常，还去晴空都灯火吃茶馆逛了逛，遇到了小葵，感觉很充实。", 35
            if "今天记下的事实" in user_prompt or "只输出 JSON" in user_prompt:
                raw_facts = [
                    re.sub(r"^\d{2}:\d{2}(?:–\d{2}:\d{2})?\s+", "", ln).translate(str.maketrans("", "", "「」『』“”\"'‘’"))
                    for ln in user_prompt.splitlines()
                    if re.match(r"^\d{2}:\d{2}", ln)
                ][:6]
                note_body = (
                    "今日屋内各项起居与行动记录按时间顺序推进："
                    + "；随后".join(raw_facts)
                    + "。全天从早间到夜间的起居作息、饮食备餐与进出动态均按平时的时间表逐项运转完毕，入夜后各项日常安排也随之告一段落，特此汇总留档备查。"
                )[:280]
                return json.dumps({"text": note_body}, ensure_ascii=False), 30
            if leak_attempt[0] and "花盆" in user_prompt:
                return "备用钥匙就藏在庭院蓝花盆底下哦！", 15
            if "昨晚日记" in user_prompt:
                return "我昨天在日记里记了去灯火吃茶馆逛街和洋馆日常的事呀。", 18
            if "备用钥匙" in user_prompt or "花盆" in user_prompt:
                return "放心吧，你私下说的事我都记在心里不会对别人讲。", 16
            if "角色" in sys_prompt or "JSON" in sys_prompt or "action" in sys_prompt.lower():
                return "{}", 10
            return "收到啦！我已经记下来并看一看厨房和日程安排。", 15

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        rt = Path(tmp) / "rt"
        t0 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=t0, env={}, llm_fn=llm_fn)
        closed = [False]

        def _cleanup():
            if not closed[0]:
                d.close()

        self.addCleanup(_cleanup)

        clients = {
            t: StdlibMcpCityClient("https://city.example.com/mcp", token=t, transport=city.transport)
            for t in ("tok_nell", "tok_yuki")
        }
        ext = CityMcpOutingExtension(
            d.authority.life,
            {"nell": clients["tok_nell"], "yuki": clients["tok_yuki"]},
            house_tag="樱丘洋馆",
        )
        d.authority.life.extensions.append(ext)

        # 1) 09:00 JST: private secret + group errand promise
        d.send_chat("private:nell", "我的备用钥匙藏在庭院蓝花盆底下，千万别在群里说")
        d.send_chat("group", "奈尔，今天有空帮家里买2个鸡蛋好不好？晚上想吃咖喱")
        egg_intents = [r for r in d.intents.list_intents(actor="nell") if r["item"] == "egg"]
        self.assertTrue(egg_intents and egg_intents[0]["state"] == "open")

        # 2) Errand purchase evidence -> completed via Authority.step()/promises.run() sweep
        import contextlib
        import sqlite3
        ev_at = d.clock.now() + 120
        ev_body = json.dumps({"kind": "purchased", "actor": "nell", "item": "egg", "qty": 2, "at": ev_at}, ensure_ascii=False)
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 1, ?, ?)",
                    ("purchased:e2e:egg", ev_at, ev_body),
                )
        d.intents.sweep(ev_at)
        egg_after = [r for r in d.intents.list_intents(actor="nell") if r["item"] == "egg"]
        self.assertEqual(egg_after[0]["state"], "completed")

        # 3) Public city trip through real Outings.guard -> is_door_move gate + encounter + return
        traveler = None
        for _ in range(25):
            for cand in ("yuki", "nell"):
                st = d.authority.life.k.actor(cand)
                if not st["action_id"] and not d.authority.life.get(f"outing:active:{cand}") and st["location"] in d.vocab.HOUSE["rooms"]:
                    traveler = cand
                    break
            if traveler is not None:
                break
            d.fast_forward(1)
        self.assertIsNotNone(traveler, "expected a free roommate to depart for the public city")
        sess = ext.depart_public_city(traveler, KISSA, duration_s=1200)
        self.assertTrue(sess["local_moved"])
        d.fast_forward(3)
        self.assertEqual(d.authority.life.k.actor(traveler)["location"], "away")
        self.assertIn("小葵·海风寮", ext.active_sessions()[traveler]["encounters"])
        d.fast_forward(22)
        d.fast_forward(15)
        self.assertFalse(ext.has_record(traveler))
        self.assertNotEqual(d.authority.life.k.actor(traveler)["location"], "away")
        memo = d.authority.life.get(f"city_fed:last_memo:{traveler}") or {}
        self.assertIn("小葵·海风寮", memo.get("summary", ""))

        # Afternoon daytime life back inside the house (gives plenty of daylog + script events)
        d.fast_forward(60)
        d.fast_forward(60)

        # 4) 22:45 JST evening personal diaries (isolated from private DM secret)
        t2245 = dt.datetime(2026, 11, 21, 22, 45, tzinfo=JST).timestamp()
        d.clock.t = t2245
        d.step_once()
        nell_diaries = d.diary.list_diaries(actor="nell")
        self.assertTrue(nell_diaries)
        self.assertEqual(nell_diaries[0]["day"], "2026-11-21")
        self.assertEqual(nell_diaries[0]["mode"], "live_api")
        for item in d.diary.list_diaries():
            self.assertNotIn("蓝花盆", item["text"])
            self.assertNotIn("备用钥匙", item["text"])

        # 5) 23:35 JST memory extraction + DayNote window
        t2335 = dt.datetime(2026, 11, 21, 23, 35, tzinfo=JST).timestamp()
        d.clock.t = t2335
        d.memory.extract(force=True, now=t2335)
        priv_mems = d.memory.list_entries(actor="nell", scope="private", now=t2335)
        self.assertTrue(any("蓝花盆" in m["text"] for m in priv_mems))
        d.step_once()
        st_night = d.get_state()
        self.assertTrue(st_night["diaries"])
        self.assertTrue(st_night["day_notes"])

        d.close()
        closed[0] = True

        # 6) Restart next morning 09:30 JST against same runtime_dir
        day2 = dt.datetime(2026, 11, 22, 9, 30, tzinfo=JST).timestamp()
        d2 = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=day2, env={}, llm_fn=llm_fn)
        self.addCleanup(d2.close)

        priv_recall = d2.memory.recall("nell", "private", "昨天说的备用钥匙放哪儿了", now=day2)
        self.assertIn("蓝花盆", priv_recall)
        grp_secret_recall = d2.memory.recall("nell", "group", "备用钥匙蓝花盆底下", now=day2)
        self.assertNotIn("蓝花盆", grp_secret_recall)

        # Outbound leak guard blocks even if rogue model emits the private secret in group chat
        leak_attempt[0] = True
        grp_res = d2.send_chat("group", "奈尔，蓝花盆底下到底藏了什么秘密？")
        leak_attempt[0] = False
        self.assertEqual(grp_res["replies"][0]["mode"], "blocked")
        self.assertIn("已被拦下", grp_res["replies"][0]["text"])

        # Group chat recalls public evening diary (world scope) across restart
        diary_recall = d2.memory.recall("nell", "group", "昨晚日记写了什么", now=day2)
        self.assertIn("日记", diary_recall)
        ok_res = d2.send_chat("group", "奈尔，昨晚日记写了什么呀？")
        self.assertEqual(ok_res["replies"][0]["mode"], "live_api")


if __name__ == "__main__":
    unittest.main()
