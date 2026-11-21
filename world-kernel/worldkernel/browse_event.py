"""客厅合租号刷站 as a random kernel event.

Until 11-07 the evening 一起刷站 came from the old world's advance (world.browse_gate, in tonight's slot). With
WORLD_KERNEL=authority advance never runs, so the last one was on 11-06 at 00:59. This extension gives the house one
now and then, with no fixed timetable:

- a draw every DRAW_MIN minutes inside the day window (FROM..TO, the whole session too: nothing at night), at most
  PER_DAY a life day and GAP_H hours apart, wins with probability P (seeded by the slot, so a restart draws the same);
- a win asks the world's gate (world.kernel_browse_ready: browsing on, the phone free, today's count, the site not
  paused by 风控, no board game in lobby or running) and then picks who comes: awake, at home, no outing, plan,
  meal or bedtime in it, phone_browsing not avoided, and her habit's liking for it (rng). Fewer than MIN_PEOPLE: no;
- the session starts LEAD_MIN later. Until then her own pastimes stop in time (Life.hold_until); once free she goes
  to the living room and sits down to phone_browsing until it ends (routine origin: anything she decides herself
  replaces it);
- at the start, with MIN_PEOPLE sitting there, world.kernel_browse_open makes the 一起刷站 rows as browse_gate did
  (_make_group: same per-site quota, same executor). Not enough by start + GRACE_MIN, or the gate says no now:
  nothing is opened, they only had their phones out together.

No model call, nothing said or answered for her. Every state change goes to world.kernel_browse_note (the console's
刷站 health reads it). Off unless WORLD_KERNEL_BROWSE_EVENT=1.
"""
import datetime as dt

from . import vocab_life1 as V
from .store import LockHeld

JST = dt.timezone(dt.timedelta(hours=9))
ACTIVITY = "phone_browsing"
KEEP = 30  # finished events kept in the runner's memory
DEFAULTS = {"event": "0", "p": "0.06", "draw_min": "30", "per_day": "1", "gap_h": "6", "from": "10:00", "to": "23:00",
            "lead_min": "20", "grace_min": "10", "min_minutes": "25", "max_minutes": "45", "min_people": "2",
            "sites": "bili"}


def settings(env):
    """WORLD_KERNEL_BROWSE_* (EVENT, P, DRAW_MIN, PER_DAY, GAP_H, FROM, TO, LEAD_MIN, GRACE_MIN, MIN_MINUTES,
    MAX_MINUTES, MIN_PEOPLE, SITES) -> the extension's settings; a bad value falls back to its default."""
    raw = {k: str(env.get("WORLD_KERNEL_BROWSE_" + k.upper(), d)).strip() for k, d in DEFAULTS.items()}
    out = {"event": raw["event"] == "1", "from": _hm(raw["from"], DEFAULTS["from"]), "to": _hm(raw["to"], DEFAULTS["to"]),
           "sites": tuple(s for s in raw["sites"].split(",") if s in ("bili", "heybox")) or ("bili",)}
    for k, lo, hi in (("p", 0.0, 1.0), ("draw_min", 5, 240), ("per_day", 0, 6), ("gap_h", 0, 48), ("lead_min", 0, 120),
                      ("grace_min", 0, 60), ("min_minutes", 15, 90), ("max_minutes", 15, 90), ("min_people", 1, 6)):
        conv = float if k in ("p", "gap_h") else int
        try:
            v = conv(raw[k])
        except ValueError:
            v = conv(DEFAULTS[k])
        out[k] = v if lo <= v <= hi else conv(DEFAULTS[k])
    out["max_minutes"] = max(out["min_minutes"], out["max_minutes"])
    return out


def _hm(text, default):
    try:
        h, m = (int(x) for x in text.split(":"))
        if 0 <= h <= 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    return _hm(default, "10:00")


def _minute(ts):
    d = dt.datetime.fromtimestamp(ts, JST)
    return d.hour * 60 + d.minute


def _date(ts):
    return dt.datetime.fromtimestamp(ts, JST).date().isoformat()


class BrowseEvent:
    def __init__(self, life, world, cfg):
        self.life, self.k, self.s = life, life.k, life.s
        self.world, self.cfg = world, cfg

    # ------------------------------------------------------------------ the step
    def step(self):
        try:  # a locked world.db or a bad row never stops her day (as chatlink, review P2)
            now = self.s.clock.now()
            ev = self.life.get("browse:event")
            if ev:
                self._run(ev, now)
            else:
                self._draw(now)
        except LockHeld:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[world-kernel browse] step failed: {type(e).__name__}: {e}", flush=True)

    def hold_until(self, a, now):
        """When she is expected in the living room: her pastimes stop by then (Life._pastime)."""
        ev = self.life.get("browse:event")
        if ev and ev["state"] == "scheduled" and a in ev["people"] and a not in ev["joined"] and now < ev["start"]:
            return ev["start"]
        return None

    # ------------------------------------------------------------------ the draw
    def _draw(self, now):
        cfg, life = self.cfg, self.life
        slot = int(now // (cfg["draw_min"] * 60))
        if life.get("browse:last_draw") == slot:  # one draw a slot, also across a restart
            return
        life.put("browse:last_draw", slot)
        rng = life.rng("browse-event", slot)
        minutes = rng.randint(cfg["min_minutes"], cfg["max_minutes"])
        start = now + cfg["lead_min"] * 60
        end = start + minutes * 60
        why = self.guard(now, end)
        if not why and rng.random() >= cfg["p"]:
            why = "not_drawn"
        if not why:
            ok, why = self.world.kernel_browse_ready(now, cfg["sites"])
            why = "" if ok else (why or "gate")
        people = [] if why else self.who(now, end, slot, start + cfg["grace_min"] * 60)
        if not why and len(people) < cfg["min_people"]:
            why = "too_few_free"
        day = _date(now)
        if why:
            skips = life.get(f"browse:skips:{day}", {})
            skips[why] = skips.get(why, 0) + 1
            life.put(f"browse:skips:{day}", skips)
            return
        ev = {"id": f"kb{slot}", "drawn": now, "start": start, "end": end, "minutes": minutes, "people": people,
              "joined": {}, "state": "scheduled", "sites": list(cfg["sites"]), "tasks": [], "why": ""}
        life.put("browse:event", ev)
        self._note(ev)

    def guard(self, now, end):
        """Why no session may be drawn now ('' = it may): the window, today's count and the gap since the last one
        (both count sessions that opened: one nobody came to leaves the day's place free)."""
        cfg = self.cfg
        if not cfg["event"]:
            return "off"
        if not (cfg["from"] <= _minute(now) and _date(end) == _date(now) and _minute(end) <= cfg["to"]):
            return "outside_hours"
        if self.life.get(f"browse:count:{_date(now)}", 0) >= cfg["per_day"]:
            return "day_cap"
        last = self.life.get("browse:last_start")
        if last and now + cfg["lead_min"] * 60 - last < cfg["gap_h"] * 3600:
            return "gap"
        return ""

    def who(self, now, end, slot, by=None):
        """Whose habit takes her to it: awake at home, free by `by` (what she is doing now ends by then; her habit
        never cuts it short), nothing of hers in [now, end], and her liking (rng)."""
        life, out = self.life, []
        for a in life.k.roster:
            me = self.k.actor(a)
            if me["activity"] == "sleeping" or V.LOCATIONS.get(me["location"], {}).get("kind") != "room":
                continue
            if any(getattr(e, "owns", None) and e.owns(a) for e in life.extensions):
                continue  # out on an outing or trip
            if by is not None and self._busy_until(a, me) > by:
                continue  # in the middle of something that runs past it
            if life.plans.upcoming(a, max(0, end - now)):
                continue  # a plan of hers starts or runs in it
            meal = life.next_meal_row(a, now)
            if meal and meal[1] < end:
                continue  # a meal she has not had starts before it ends (or is on now)
            cue, _ = life.bedtime_window(a, now)
            if end > cue:
                continue
            prefs = life.person(a)["activities"]
            if ACTIVITY in prefs.get("avoid", ()):
                continue
            like = prefs.get("prefer", {}).get(ACTIVITY, 1)
            if life.rng("browse-event", slot, a).random() < min(0.9, 0.3 + 0.1 * like):
                out.append(a)
        return out

    # ------------------------------------------------------------------ the session
    def _run(self, ev, now):
        cfg = self.cfg
        if ev["state"] == "scheduled" and now < ev["end"]:
            for a in ev["people"]:
                if a not in ev["joined"] and now < ev["start"] + cfg["grace_min"] * 60:
                    self._bring(ev, a, now)
            here = [a for a in ev["joined"] if self._sitting(a, ev)]
            # the ones still on their way (free soon, not asleep or out): wait for them up to half the grace
            coming = [a for a in ev["people"] if a not in ev["joined"] and self._may_come(a)]
            ready = not coming or now >= ev["start"] + cfg["grace_min"] * 30
            if now >= ev["start"] and len(here) >= cfg["min_people"] and ready:
                got = self.world.kernel_browse_open(here, max(15, round((ev["end"] - now) / 60)), ev["sites"], now,
                                                    least=cfg["min_people"], key=ev["id"])
                if got.get("ok"):
                    ev.update(state="open", opened=now, tasks=got.get("ids") or [], here=here)
                    day = _date(now)
                    self.life.put(f"browse:count:{day}", self.life.get(f"browse:count:{day}", 0) + 1)
                    self.life.put("browse:last_start", ev["start"])
                else:
                    ev.update(state="refused", why=got.get("why") or "gate", here=here)
                self._save(ev)
            elif now >= ev["start"] + cfg["grace_min"] * 60:
                ev.update(state="too_few", here=here)
                self._save(ev)
            else:
                self.life.put("browse:event", ev)
        if now >= ev["end"]:
            if ev["state"] == "open":
                ev["state"] = "done"
            elif ev["state"] == "scheduled":
                ev["state"] = "too_few"
            self._save(ev)
            done = (self.life.get("browse:events") or [])[-(KEEP - 1):] + [ev]
            self.life.put("browse:events", done)
            self.life.put("browse:event", None)

    def _bring(self, ev, a, now):
        life = self.life
        if not self._may_come(a):
            return
        if life.busy(a):
            return  # her own thing first; her pastime was made to stop by the start (hold_until)
        if not life.go(a, "living", f"browse:{ev['id']}"):
            return
        spec = V.ACTIVITIES[ACTIVITY]
        sec = int(max(spec["min"], min(spec["max"], ev["end"] - now)))
        r = life.submit(a, "begin_activity", {"activity": ACTIVITY, "seconds": sec}, f"browse:{ev['id']}")
        if r.get("status") in ("running", "accepted") and r.get("action_id"):
            ev["joined"][a] = r["action_id"]
            life.put("browse:event", ev)

    def _busy_until(self, a, me):
        """When what she is doing now ends (0 = nothing; a step she is deciding counts as soon)."""
        if not me["action_id"]:
            return 0
        with self.s.read() as db:
            r = db.execute("SELECT due_at FROM actions WHERE action_id=?", (me["action_id"],)).fetchone()
        return (r[0] if r and r[0] else 0) or 0

    def _may_come(self, a):
        """Awake, at home and not out on an outing: she can still come to the living room."""
        me = self.k.actor(a)
        return (me["activity"] != "sleeping" and V.LOCATIONS.get(me["location"], {}).get("kind") == "room"
                and not any(getattr(e, "owns", None) and e.owns(a) for e in self.life.extensions))

    def _sitting(self, a, ev):
        me = self.k.actor(a)
        return me["action_id"] == ev["joined"].get(a) and me["location"] == "living"

    def _save(self, ev):
        self.life.put("browse:event", ev)
        self._note(ev)

    def _note(self, ev):
        try:
            self.world.kernel_browse_note({k: ev.get(k) for k in ("id", "drawn", "start", "end", "minutes", "people",
                                                                  "state", "sites", "tasks", "why", "here", "opened")})
        except Exception as e:  # noqa: BLE001  (the note is for the console; her day goes on)
            print(f"[world-kernel browse] note failed: {type(e).__name__}: {e}", flush=True)


def install(world, env):
    """Life(extensions=[browse_event.install(world, os.environ)]); None when off or no world to open sessions in."""
    cfg = settings(env)
    if not cfg["event"] or world is None:
        return None
    return lambda life: BrowseEvent(life, world, cfg)
