"""Her decisions in life1: the live prompt (one call = one decision of hers) and a persona-shaped fake model
for the fast-forward week. Both return Cognition's {"steps": [...]} shape; every step is a kernel command.
"""
import datetime as dt
import hashlib
import json
import random
import re

from . import vocab_life1 as V

JST = dt.timezone(dt.timedelta(hours=9))


def parse_when(text):
    """'2026-11-03 14:00' (JST) -> epoch seconds, or None."""
    try:
        return dt.datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()
    except ValueError:
        return None


# the steps life_format offers; only these names are recognised in the shorter shapes models tend to write
STEP_TYPES = ("move", "begin_activity", "cook", "eat", "change_clothes", "pick_shelf_garment", "invite",
              "respond_invite", "sleep", "rest", "wait", "say")
TOP_LEVEL = ("guide_id", "uses", "about")  # step fields that sit beside args, not inside them
# key names a dropped step may be logged with; any other key is logged as "<other>" (a key can be a sentence)
KNOWN_KEYS = frozenset(STEP_TYPES + TOP_LEVEL + (
    "action_type", "action", "type", "args", "steps", "to", "activity", "seconds", "party", "recipe", "item",
    "resource_id", "items", "occasion", "asset_id", "kind", "what", "location", "start", "end", "inv_id",
    "answer", "reason"))


def canonical(s):
    """One step as the model wrote it -> {"action_type", "args", ...} or None. Accepted, all unambiguous:
    the kernel shape; {"action"|"type": name, ...}; {name: {args}} with exactly one key that is a step name;
    and fields written beside action_type instead of inside args. Anything else is None (reported)."""
    if not isinstance(s, dict):
        return None
    name = s.get("action_type")
    if name is None:
        for k in ("action", "type"):
            if isinstance(s.get(k), str) and s[k] in STEP_TYPES:
                name, s = s[k], {x: v for x, v in s.items() if x != k}
                break
        else:
            if len(s) == 1:
                (k, v), = s.items()
                if k in STEP_TYPES and isinstance(v, dict):
                    name, s = k, dict(v)
    else:
        s = {x: v for x, v in s.items() if x != "action_type"}
    if not isinstance(name, str):
        return None
    if any(k in s and not isinstance(s[k], list) for k in ("uses", "about")):
        return None  # Cognition would refuse the whole answer and leave the decision unresolved for good
    args = s.pop("args", None)
    if args is None:
        args = {k: s.pop(k) for k in list(s) if k not in TOP_LEVEL}
    if not isinstance(args, dict):
        return None
    return dict(s, action_type=name, args=dict(args))


def shape(s):
    """What a dropped step looked like, without its content (her words never reach the log)."""
    if not isinstance(s, dict):
        return [type(s).__name__]
    return sorted({k if k in KNOWN_KEYS else "<other>" for k in s})[:6]


def split_steps(steps):
    """-> (kernel steps, dropped shapes). Times as strings become timestamps; values are not guessed, the
    kernel refuses what it cannot do."""
    out, dropped = [], []
    for raw in steps if isinstance(steps, list) else [steps]:
        s = canonical(raw)
        if s is None:
            dropped.append(shape(raw))
            continue
        if s["action_type"] == "invite":
            for src, dst in (("start", "start_at"), ("end", "end_at")):
                if src in s["args"]:
                    t = parse_when(s["args"].pop(src))
                    if t is not None:
                        s["args"][dst] = t
        out.append(s)
    return out, dropped


def normalize(steps):
    return split_steps(steps)[0]


def read_answer(text):
    """Model text -> (steps, unusable or None). unusable says why an answer gave no usable step although it
    was not an empty choice; it holds only key names and counts."""
    text = text or ""
    if "{" not in text:
        return [], {"reason": "no_json"}
    got, dec = None, json.JSONDecoder()
    for i in [j for j, ch in enumerate(text) if ch == "{"][:20]:  # the first object that parses
        try:
            got = dec.raw_decode(text, i)[0]
            break
        except ValueError:
            continue
    if got is None:
        return [], {"reason": "bad_json"}
    if not isinstance(got, dict) or "steps" not in got:
        return [], {"reason": "no_steps", "shapes": [shape(got)]}
    raw = got["steps"]
    if raw == [] or raw is None:
        return [], None
    if not isinstance(raw, (list, dict)):
        return [], {"reason": "bad_steps", "shapes": [shape(raw)]}
    steps, dropped = split_steps(raw)
    return steps, ({"dropped": len(dropped), "shapes": dropped[:4]} if dropped else None)


# helping in the kitchen makes no food (no effect in the catalog); offered at a meal decision, four of them
# chose it on 11-03 while lunch was still to be made, and missed lunch busy. eating_snack is the same trap (nothing
# is eaten; a snack is an eat of a snack she sees, review F4), and so are hotpot_night (一起吃火锅, in the kitchen) and
# eat_out (在外面吃, which also takes her out of the house): no effect, nothing is eaten (review P2-2).
NOT_AT_MEALS = ("kitchen_help", "eating_snack", "hotpot_night", "eat_out")

# what each reason to think means, in words (on 11-03 she saw only a code like free_time, and never invited
# anyone: nothing told her this was the time of day to ask the others along)
WHY = {
    "free_time": "答应过的事可以挑现在方便的去做；不方便就算了，别硬凑。现在是你今天的空闲时间（一天问你一次）。可以自己做喜欢的事；也可以约室友一起："
                 "在家里一起玩游戏、看电影、喝茶，或者一起出门。约人用 invite，roommates_now 是你知道的她们现在在哪、"
                 "在做什么、忙到几点、什么时候已经有安排；答不答应由她们自己决定。",
    "head_cook": "这顿饭由你来做：看 meal 里有几个人要吃、几点截止，从 cook_options 里挑菜做。",
    "scheduled_meal_needs_preparation": "到饭点了，饭还没好。",
    "scheduled_meal_preparation_waiting": "到饭点了，饭还在做。",
    "scheduled_meal_own_choice": "到饭点了，吃不吃、吃什么由你自己定。",
    "bedtime": "到你平时睡觉的时间了：去睡，或者再晚一点睡。",
    "invitation": "有人邀请你（invitations 里的这个 inv_id），用 respond_invite 回答 yes 或 no。",
    # 房主化身走进小屋: he is really there, typed by himself on the house page; nothing goes to QQ
    "owner_entered": "房主走进了你在的房间（here 里有 owner）。想打招呼就用 say（不写 to 是对屋里说，to 写 \"owner\" 是只对房主说），"
                     "也可以接着做你的事，要不要停下来由你决定。",
    "owner_said": "房主在你在的房间里说了话（recent 里 kind=owner_input_received 的 text；"
                  "房主把东西递给别人也会在 recent 里，kind=owner_gave）。想回应就用 say，也可以只是听着、接着做事。",
    "owner_gave": "房主把一样东西递给了你（recent 里 kind=owner_gave，to 是你；它现在在你手上，items_here 里有）。"
                  "可以道谢（say），想现在吃就用 eat（resource_id 用那一行的），也可以先收着。",
    # 聊天介入生活: what he said on QQ and what she answered there
    "owner_chat": "房主刚在 QQ 上和你聊过（life.qq_chat 里是房主的原话、你自己的回话，和你在聊天里答应过的打算）。"
                  "现在想想在家里要不要因此做点什么：答应了早点睡就去睡（sleep，在自己卧室），答应了和室友一起做什么就用 invite "
                  "约好时间和地点，说了要去做饭、洗澡、出门就照做。没答应的、做不到的、和家里无关的就 {\"steps\": []}。"
                  "房主本人不在家里，别替房主安排他要做的事。",
}


def why(kind):
    """The words for a reason to think, or "" for one without."""
    return WHY.get(kind, WHY.get(kind.split(":", 1)[0], ""))


def life_format(me, life):
    room = V.bed(me)
    meal = bool(life.get("meal"))
    acts = "；".join(f"{k}（{a['label']}，{a['min'] // 60}-{a['max'] // 60}分钟，在 {'、'.join('自己卧室' if w == 'own_room' else w for w in a['where'])}）"
                    for k, a in V.ACTIVITIES.items() if not (meal and k in NOT_AT_MEALS))
    places = "；".join(f"place:{k}（{p['label']}，单程{p['travel']['minutes']}分钟）" for k, p in V.PLACES.items())
    return ("只输出一个 JSON：{\"steps\": [...]}，最多 4 步，不想做什么就 {\"steps\": []}。\n"
            "每一步都写成 {\"action_type\": 步骤名, \"args\": {参数}}，例如"
            " {\"steps\": [{\"action_type\": \"move\", \"args\": {\"to\": \"kitchen\"}}]}。可用步骤：\n"
            f"- {{\"action_type\": \"move\", \"args\": {{\"to\": \"kitchen|living|study|yard|bath|{room}|place:<id>\"}}}}"
            f"（{room} 是你的卧室；别人的卧室不能进）\n"
            "- {\"action_type\": \"begin_activity\", \"args\": {\"activity\": 活动, \"seconds\": 秒, \"party\": 可选 plan_id}}"
            "（要先走到能做它的地方；在家里做的，到 next_meal 开饭就停，seconds 会截短，最短按活动下限）\n"
            f"  活动：{acts}\n"
            f"  地点：{places}\n"
            "- {\"action_type\": \"cook\", \"args\": {\"recipe\": cook_options 里的一个 id}}（在厨房；cook_options 写着几人份、"
            "要做几分钟，只列现在开做还来得及的菜）\n"
            "- {\"action_type\": \"eat\", \"args\": {\"item\": 东西, \"resource_id\": id}}\n"
            "- {\"action_type\": \"change_clothes\", \"args\": {\"items\": [wardrobe_choices 里自己的衣服 id], "
            "\"occasion\": \"home|out|formal|sleep|sport|onsen\"}}\n"
            "- {\"action_type\": \"pick_shelf_garment\", \"args\": {\"asset_id\": shelf_picks 里的货架服装 id}}"
            "（从公共货架挑喜欢的衣服或套装收进自己衣柜，再换装）\n"
            "- {\"action_type\": \"invite\", \"args\": {\"to\": [室友 id], \"kind\": \"meal|activity|outing|trip\", \"what\": 活动或菜,"
            " \"location\": 地点, \"start\": \"YYYY-MM-DD HH:MM\", \"end\": 可选}}"
            "（约室友：to 写 roommates 里的 id；activity=在家里一起做上面的一个活动，what 写活动 id，location 写房间；"
            "outing=一起出门，location 写 place:<id>，what 写在那儿做的活动；meal=一起吃饭；trip=一日游或过夜。"
            "start 要晚于现在。她们会自己回答）\n"
            "- {\"action_type\": \"respond_invite\", \"args\": {\"inv_id\": id, \"answer\": \"yes|no\", \"reason\": 一句理由}}\n"
            "- {\"action_type\": \"sleep\", \"args\": {\"seconds\": 秒}}（在自己卧室或旅馆）\n"
            "- {\"action_type\": \"rest\", \"args\": {\"seconds\": 秒}}；{\"action_type\": \"wait\", \"args\": {\"seconds\": 秒}}\n"
            "- {\"action_type\": \"say\", \"args\": {\"to\": 可选}, \"about\": []}（只写对谁说，不写台词；"
            "房主在场时 to 可以写 \"owner\"）\n"
            "invitations 里带 yes_refused 的，是你上次答应了但没成，后面是原因"
            "（例如 outing_schedule_conflict：和你已有的外出撞了时间），请重新回一次。\n"
            "只替自己决定；邀请别人只是问，对方自己答。不要编没有的东西、没去过的地方。")


class LiveLifeModel:
    """ctx -> {"steps"} via chat(system, user) -> (text, tokens)."""

    def __init__(self, chat, selves):
        self.chat, self.selves, self.calls = chat, selves, 0

    def __call__(self, ctx):
        self.calls += 1
        me, life = ctx["actor"], ctx.get("life", {})
        system = (f"你是 AI小屋里的{self.selves.get(me, me)}。现在是日本时间 {life.get('now')}（星期{life.get('weekday')}）。"
                  "只根据给你的、你自己看到和知道的东西决定你自己接下来做什么。输出必须是 JSON。")
        if ctx.get("purpose") == "express":
            seen = {k: ctx.get(k) for k in ("location", "here", "recent")}
            names, to = life.get("names") or {}, ctx.get("to")
            to = [to] if isinstance(to, str) else list(to or ())
            who = "、".join(f"{names.get(t, t)}" for t in to) or "这个房间里的人"
            # the names to call them by (on 11-03 she had ids only and wrote 星莉 for yuki)
            call = ("名字对照（说话时用名字，不要写 id）：" + "，".join(f"{k}={v}" for k, v in names.items()) + "\n") \
                if names else ""
            user = (f"你决定对{who}说一句话。对方也能看到的：{json.dumps(seen, ensure_ascii=False)}\n{call}"
                    '用你自己的口吻说一句，不超过 40 字，只输出 {"text": "..."}。')
            text, tokens = self.chat(system, user)
            m = re.search(r"\{.*\}", text, re.S)
            return {"text": str((json.loads(m.group(0)) if m else {}).get("text") or "")[:120], "tokens": tokens}
        seen = {k: ctx.get(k) for k in ("location", "activity", "body", "here", "items_here", "recent")}
        # show one usable representative per kind, totals and batch count; do not repeat 75 legacy
        # resources in every kitchen decision. Only rows already visible to this actor are grouped.
        grouped = {}
        for row in ctx.get('items_here') or ():
            key = row['item']
            if key not in grouped:
                grouped[key] = dict(row, variants=1)
            else:
                grouped[key]['qty'] += row['qty']
                grouped[key]['variants'] += 1
        seen['items_here'] = list(grouped.values())
        seen["life"] = life
        kind = ctx.get("decision") or ""
        user = (f"你现在的情况：\n{json.dumps(seen, ensure_ascii=False)}\n让你想一想的原因：{kind}"
                + (f"——{why(kind)}" if why(kind) else "") + "\n\n" + life_format(me, life))
        text, tokens = self.chat(system, user)
        steps, unusable = read_answer(text)
        out = {"steps": steps[:4], "latency": 0, "tokens": tokens}
        if unusable:
            out["unusable"] = unusable
        return out


NIGHT_OWLS = {"alma": 0.7, "mina": 0.8, "wanda": 0.5, "yuki": 0.4, "elsa": 0.15, "nell": 0.1}
# the fake model's no after the world refused her yes, by the refusal's reason
NO_BECAUSE = {"outing_schedule_conflict": "那天已经有安排了", "monthly_overnight_limit": "这个月已经出去住过一次了",
              "insufficient_funds": "这个月钱不太够", "avoid_place_in_rain": "下雨天就不去那儿了"}


class FakeLifeModel:
    """Persona-shaped, deterministic stand-in for the fast-forward week (no network, zero cost)."""

    def __init__(self, seed="life1"):
        self.seed, self.calls = seed, 0

    def rng(self, *parts):
        return random.Random(hashlib.sha256(":".join(map(str, (self.seed,) + parts)).encode()).hexdigest())

    def __call__(self, ctx):
        self.calls += 1
        me, life, kind = ctx["actor"], ctx.get("life", {}), ctx.get("decision") or ""
        if ctx.get("purpose") == "express":
            last = next((e for e in reversed(ctx.get("recent") or ()) if e.get("actor") == "owner"), None)
            if last and last["kind"] == "owner_gave" and last.get("to") == me:
                return {"text": "谢谢房主！", "tokens": 0}
            if last and last["kind"] == "owner_input_received":
                return {"text": "嗯，我在听。", "tokens": 0}
            if last and last["kind"] == "owner_entered":
                return {"text": "房主来啦。", "tokens": 0}
            return {"text": "嗯。", "tokens": 0}
        r = self.rng(me, kind, life.get("now"))
        steps = []
        prefs = V.PEOPLE[me]["activities"]["prefer"]
        if kind in ("owner_entered", "owner_said"):  # 房主化身: a word to the room
            steps = [{"action_type": "say", "args": {}, "about": []}]
        elif kind == "owner_gave":  # thanks him, then eats it now or keeps it
            steps = [{"action_type": "say", "args": {"to": "owner"}, "about": []}]
            mine = [e for e in ctx.get("recent") or () if e.get("kind") == "owner_gave" and e.get("to") == me]
            if mine and r.random() < 0.5:
                steps.append({"action_type": "eat", "args": {"item": mine[-1].get("item"),
                                                             "resource_id": mine[-1].get("resource_id")}})
        elif kind == "owner_chat":  # the fake keeps one promise kind: she said she would go to bed early
            talk = life.get("qq_chat") or []
            plans = " ".join(i["text"] for t in talk for i in t.get("你的打算", []) if me in i.get("答应了的", []))
            if "睡" in plans:
                if ctx.get("location") != V.bed(me):
                    steps.append({"action_type": "move", "args": {"to": V.bed(me)}})
                steps.append({"action_type": "sleep", "args": {"seconds": 8 * 3600}})
        elif kind.startswith("invitation:"):
            inv = next((i for i in life.get("invitations", ()) if i["inv_id"] == kind.split(":", 1)[1]), None)
            if inv:
                like = prefs.get(inv["what"], 2) if inv["kind"] == "activity" else 3
                # a yes the world refused: she knows why now and says she cannot make it
                yes = not inv.get("yes_refused") and r.random() < (0.9 if inv["kind"] == "meal" else 0.35 + 0.12 * like)
                no = NO_BECAUSE.get(inv["yes_refused"], "去不了了") if inv.get("yes_refused") else "这会儿想自己待着"
                steps = [{"action_type": "respond_invite", "args": {"inv_id": inv["inv_id"], "answer": "yes" if yes else "no",
                                                                      "reason": "好呀" if yes else no}}]
        elif kind.startswith("head_cook:"):
            opts = life.get("cook_options") or []
            need = len(life.get("eaters") or ()) or 1
            good = V.PEOPLE[me]["cooking"]["good_at"]
            ranked = sorted(opts, key=lambda o: (V.RECIPES[o]["makes"][o] >= need, o in good, r.random()), reverse=True)
            pick = ranked[0] if ranked else None
            others = [e for e in life.get("eaters", ()) if e != me]
            if others:
                t = dt.datetime.strptime(life["now"], "%Y-%m-%d %H:%M") + dt.timedelta(
                    minutes=V.RECIPES[pick]["seconds"] // 60 if pick else 20)
                steps.append({"action_type": "invite", "args": {"to": others, "kind": "meal", "what": pick or "",
                                                                "location": "kitchen",
                                                                "start": t.strftime("%Y-%m-%d %H:%M")}})
            if pick:
                steps.append({"action_type": "cook", "args": {"recipe": pick}})
        elif kind == "free_time":
            now = dt.datetime.strptime(life["now"], "%Y-%m-%d %H:%M")
            roll = r.random()
            if roll < 0.12 and now.weekday() in (4, 5):  # Fri/Sat: a day trip tomorrow (planned a day ahead)
                trips = [k for k, p in V.PLACES.items() if p.get("tier") == "daytrip"]
                if trips:
                    pid = r.choice(trips)
                    day = (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
                    what = [a for a in V.PLACES[pid]["activities"] if a != "shopping"][0]
                    steps = [{"action_type": "invite", "args": {
                        "to": r.sample(life.get("roommates", []), k=r.randint(1, 3)), "kind": "trip", "what": what,
                        "location": "place:" + pid, "start": f"{day} 09:00", "end": f"{day} 17:00"}}]
            elif roll < 0.35 and now.hour < 14:  # an afternoon out nearby or in town
                outs = [k for k, p in V.PLACES.items() if p.get("tier") in ("near", "town", "city")
                        and any(a != "shopping" for a in p["activities"])]
                pid = r.choice(outs)
                what = r.choice([a for a in V.PLACES[pid]["activities"] if a != "shopping"])
                day = now.strftime("%Y-%m-%d")
                start, end = ("13:00", "18:00") if V.PLACES[pid]["tier"] == "city" else ("15:00", "17:30")
                steps = [{"action_type": "invite", "args": {
                    "to": r.sample(life.get("roommates", []), k=r.randint(1, 2)), "kind": "outing", "what": what,
                    "location": "place:" + pid, "start": f"{day} {start}", "end": f"{day} {end}"}}]
            elif r.random() < 0.55:
                party = [k for k, s in V.ACTIVITIES.items() if s["party"] and any(w in V.HOUSE["rooms"] for w in s["where"])]
                what = max(party, key=lambda k: (prefs.get(k, 1), r.random()))
                who = r.sample(life.get("roommates", []), k=r.randint(1, 3))
                day = life["now"][:10]
                start = f"{day} {r.choice(['15:00', '20:00', '20:30', '21:00'])}"
                where = [w for w in V.ACTIVITIES[what]["where"] if w in V.HOUSE["rooms"]][0]
                if start[11:] > life["now"][11:]:
                    steps = [{"action_type": "invite", "args": {"to": who, "kind": "activity", "what": what,
                                                                "location": where, "start": start}}]
        elif kind == "bedtime":
            if r.random() < NIGHT_OWLS.get(me, 0.3):
                # staying up with her phone where that is allowed (living or her room): from the study or the yard
                # she goes to her room first (the stale retry used to hide 6 guard refusals a week)
                if not V.activity_allowed_at("phone_browsing", me, ctx.get("location")):
                    steps.append({"action_type": "move", "args": {"to": V.bed(me)}})
                steps.append({"action_type": "begin_activity", "args": {"activity": "phone_browsing", "seconds": 1800}})
        elif kind.startswith("scheduled_meal") or kind.startswith("hungry"):
            shown = life.get("cook_options")
            if shown is not None:  # life1 shows what she could still make in time: a quick one, or wait
                good = V.PEOPLE[me]["cooking"]["good_at"]
                opts = sorted(shown, key=lambda o: (V.RECIPES[o]["seconds"], o not in good))
            else:
                opts = [x for x in ("omelette", "omelette_rice", "yakisoba", "cooked_rice") if x in V.RECIPES]
            if opts:
                steps = ([] if ctx.get("location") == "kitchen" else [{"action_type": "move", "args": {"to": "kitchen"}}])
                steps.append({"action_type": "cook", "args": {"recipe": opts[0]}})
        return {"steps": normalize(steps), "latency": 30, "tokens": 0}
