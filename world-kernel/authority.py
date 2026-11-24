"""World kernel as the authority (WORLD_KERNEL=authority).

shared-services starts this instead of the shadow. The life1 runner lives the six roommates' day on the wall clock
in its own db (runtime/world-kernel/life1.db); after every step the projector writes where she is, what she is
doing, what happened today and what she wears into world.db, which the old world now only reads for those rows
(world.KERNEL_AUTHORITY). The model is the same OpenAI-compatible endpoint the shadow uses. Rolling back = WORLD_KERNEL=shadow (or
"") + recreate: the old world takes over again from the last projected rows.
"""
import os
import threading
import time
import traceback

import shadow  # packaged via py-modules alongside worldkernel
from worldkernel import daynote  # 今天的小屋, one note a night (WORLD_KERNEL_DAY_NOTE=1; off by default)
from worldkernel.store import Store, WallClock, LockHeld
from worldkernel.life import Life
from worldkernel.life_model import LiveLifeModel
from worldkernel.legacy import Projector
from worldkernel import avatar, browse_event, chatlink, city_bridge, city_client, inventory, outings, promises

HOLDER = "shared-services-authority"
WORLD = "ai-house/life1"
STEP_S = 60


def log(msg):
    print(f"[world-kernel authority] {msg}", flush=True)


class Authority:
    def __init__(self, path, world_db, chat, selves, clock=None, model=None, migrate_items=None, day_note=None,
                 browse=None, env=None, vocab=None, extra_extensions=(), intents_mod=None, note_chat=None,
                 day_note_retries=0):
        self.clock = clock or WallClock()
        self.store = Store(path, self.clock, world_id=WORLD, synthetic=False, holder=HOLDER)
        self.store.acquire()
        fresh = not self.store.meta("seeded")
        if migrate_items is None:  # on unless WORLD_KERNEL_MIGRATE_ITEMS=0
            migrate_items = os.environ.get("WORLD_KERNEL_MIGRATE_ITEMS", "1") != "0"
        # 客厅合租号刷站 as a random kernel event, opened in the old world's browse table (browse = the world
        # module, world.kernel_browse_*); off unless WORLD_KERNEL_BROWSE_EVENT=1
        eff_env = os.environ if env is None else env
        browsing = browse_event.install(browse, eff_env)
        city_weather = None
        if eff_env.get("WORLD_KERNEL_CITY_WEATHER", "0") == "1" and eff_env.get("CITY_BRIDGE_KEY"):
            city_weather = city_client.CityWeatherProvider(
                self.store,
                city_client.CityBridgeHttpClient(
                    eff_env.get("CITY_BRIDGE_URL", "http://127.0.0.1:17654/city/bridge/v1"),
                    eff_env["CITY_BRIDGE_KEY"],
                ),
                weather_source=eff_env.get("WORLD_KERNEL_WEATHER_SOURCE", "open-meteo"),
            )
        self.life = Life(self.store, model=model or LiveLifeModel(chat, selves),
                         weather=city_weather,
                         seed_reality=shadow.legacy_reality(world_db) if fresh else None,
                         seed_items=inventory.legacy_items(world_db) if fresh and migrate_items else None,
                         extensions=[outings.install, avatar.install(world_db), chatlink.install(world_db)]
                         + ([browsing] if browsing else []) + list(extra_extensions),
                         vocab=vocab)
        self.city_bridge = city_bridge.install_from_env(self.life, chat=chat, selves=selves, env=eff_env,
                                                        weather_provider=city_weather)
        if self.city_bridge is not None:
            self.life.extensions.append(self.city_bridge)
        self.proj = Projector(self.life, world_db)
        self.world_db = world_db
        self.promises = None
        if promises.enabled(eff_env):
            if intents_mod is None:
                try:
                    import intents as intents_mod
                except ImportError:
                    from worldkernel.intents_lite import IntentsLite
                    intents_mod = IntentsLite(world_db, vocab=getattr(self.life, "V", vocab))
            intents_mod.init()
            self.promises = promises.Promises(self.life, world_db, self.proj, intents_mod, env=eff_env)
            self.life.promise_source = self.promises
            self.life.extensions.append(self.promises)
        last = self.store.meta("last_step")
        if last and not fresh:  # a restart: settle what ended while we were down, decide nothing for the gap
            self.life.k.resume(float(last))
        if self.city_bridge is not None and not fresh:
            try:
                self.city_bridge.recover()
            except LockHeld:
                raise
            except Exception as e:
                log(f"city_bridge recover deferred: {type(e).__name__}: {e}")
        if day_note is None:
            day_note = daynote.enabled(eff_env)
        # the night's note may be asked through a route of its own (slower limits than her decisions) and asked again
        # when a call fails; by default it is the same chat as everything else and asked once
        self.note = (daynote.DayNote(self.life, world_db, note_chat or chat, log=log, retries=day_note_retries)
                     if day_note and chat else None)

    def close(self):
        """Release the scheduler lease if still held and close the kernel Store."""
        try:
            if self.store.epoch is not None:
                self.store.release()
        except Exception:
            pass
        self.store.close()

    def step(self):
        # owner input first; a failing queue (a locked world.db, a bad row) never stops her day
        for queue in (inventory.process_food_queue,
                      inventory.process_owner_stock_queue):  # the owner's add/set/del from the console
            try:
                queue(self.life.k, self.world_db)
            except LockHeld:
                raise
            except Exception as e:
                log(f"{queue.__name__} failed: {type(e).__name__}: {e}")
        self.life.step()
        self.proj.run()
        if self.promises:
            try:
                self.promises.run()
            except LockHeld:
                raise
            except Exception as e:
                log(f"promises failed: {type(e).__name__}")
        self.store.set_meta("last_step", self.clock.now())
        if self.note:  # after her minute is done; a failing note never stops her day and is not retried
            try:
                self.note.step()
            except LockHeld:
                raise
            except Exception as e:
                log(f"day note failed: {type(e).__name__}: {e}")

    def owner_food(self, principal, channel, message_id, text):
        """Explicit delivery command from a trusted adapter with verified owner identity.

        Takes the full list before any chat preview truncation. A shopping wish or ordinary message must not
        call this method. Does not send QQ, invoke a model or change production configuration.
        """
        return inventory.receive_food(self.life.k, principal, channel, message_id, text)

    def run_forever(self, stop=None):
        fails = 0
        while not (stop and stop.is_set()):
            try:
                self.step()
                fails = 0
            except LockHeld as e:
                log(f"lock lost, stopping: {e}")
                return
            except Exception as e:  # never take shared-services down; the old rows stay as last projected
                fails += 1
                log(f"step failed ({fails}): {type(e).__name__}: {e}")
                if fails >= 5:
                    traceback.print_exc()
                    time.sleep(600)
                    fails = 0
            time.sleep(STEP_S)


def start(runtime_dir, world_db, selves, browse=None):
    model = os.environ.get("WORLD_KERNEL_MODEL", "example/chat-model")
    base = os.environ.get("WORLD_KERNEL_BASE_URL", "http://127.0.0.1:8000")
    os.makedirs(runtime_dir, exist_ok=True)

    def chat(system, user):
        return shadow.chat_completion(system, user, model, base)

    def run():
        try:
            a = Authority(os.path.join(runtime_dir, "life1.db"), world_db, chat, selves, browse=browse)
            log(f"running: catalog life1, model {model}, cap {a.life.budget.limits}, "
                f"day note {'on' if a.note else 'off'}, "
                f"browse event {'on' if any(isinstance(e, browse_event.BrowseEvent) for e in a.life.extensions) else 'off'}")
            a.run_forever()
        except Exception as e:
            log(f"could not start: {type(e).__name__}: {e}")
            traceback.print_exc()

    t = threading.Thread(target=run, name="world-kernel-authority", daemon=True)
    t.start()
    return t
