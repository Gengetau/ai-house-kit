"""Clothes (life1): every garment is a fact with a source; what she wears is a state; changing is an action.

Garments come from her starting wardrobe (people/<actor>.json), the uncategorized public shelf (pick_shelf_garment),
a purchase (buy -> add()) or a gift (add()). An outfit is a list of her own garment ids; change_clothes is an
instant kernel action, so who changed into what and when is an event. dress() is her L0 habit: by occasion,
temperature and liking, avoiding what she wore in the last two days. It never dresses anyone else.
"""
import json
import os

from .store import dumps
from .kernel import Refused
from . import vocab_life1 as V
from .wardrobe_render import (
    BlobIntegrityError,
    KERNEL_OUTBOX_SCHEMA,
    build_swap_snapshot,
    freeze_file,
    resolve_asset_image_path,
)
from .statuscard import outfit_id

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS garments (garment_id TEXT PRIMARY KEY, actor TEXT NOT NULL, label TEXT, category TEXT,"
    " color TEXT, material TEXT, temp_lo REAL, temp_hi REAL, occasions TEXT, liking INTEGER, source TEXT,"
    " state TEXT NOT NULL DEFAULT 'clean', created_at REAL)",
    "CREATE TABLE IF NOT EXISTS outfits (actor TEXT PRIMARY KEY, items TEXT NOT NULL, occasion TEXT, since REAL,"
    " event_id INTEGER)",
    "CREATE TABLE IF NOT EXISTS outfit_log (actor TEXT, items TEXT, occasion TEXT, at REAL, event_id INTEGER)",
)
SINGLE = ("top", "bottom", "dress", "outer", "shoes", "sleepwear", "loungewear", "sportswear", "swimwear", "yukata")
BODY_COVER = (("dress",), ("top", "bottom"), ("loungewear",), ("sleepwear",), ("sportswear",), ("yukata",), ("swimwear",))


def _load_shelf_assets(shelf_assets):
    if shelf_assets is None:
        path = os.environ.get("WARDROBE_SHELF_ASSETS", "").strip()
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = getattr(V, "GARMENT_ASSETS", {})
    else:
        raw = shelf_assets
    if isinstance(raw, dict) and isinstance(raw.get("assets"), dict):
        return dict(raw["assets"])
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_render_actors(render_actors):
    if render_actors is not None:
        return frozenset(a for a in render_actors if isinstance(a, str) and a.strip())
    raw = os.environ.get("WARDROBE_RENDER_ACTORS", "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


class Wardrobe:
    def __init__(
        self,
        kernel,
        people,
        *,
        shelf_assets=None,
        asset_root=None,
        profiles_dir=None,
        blob_root=None,
        render_actors=None,
        require_display_look=True,
        require_garment_assets=True,
    ):
        self.k, self.s, self.people = kernel, kernel.s, people
        self.shelf_assets = _load_shelf_assets(shelf_assets)
        self.asset_root = asset_root if asset_root is not None else os.environ.get("WARDROBE_ASSET_ROOT") or None
        self.profiles_dir = profiles_dir if profiles_dir is not None else os.environ.get("WARDROBE_PROFILES_DIR") or None
        self.blob_root = blob_root if blob_root is not None else os.environ.get("WARDROBE_BLOB_ROOT") or None
        self.render_actors = _parse_render_actors(render_actors)
        self.require_display_look = bool(require_display_look)
        self.require_garment_assets = bool(require_garment_assets)
        with self.s.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
            for stmt in KERNEL_OUTBOX_SCHEMA:
                c.execute(stmt)
            now = self.s.clock.now()
            for actor, p in people.items():
                for g in p.get("wardrobe", ()):
                    c.execute("INSERT OR IGNORE INTO garments VALUES(?,?,?,?,?,?,?,?,?,?,?,'clean',?)",
                              (g["id"], actor, g["label"], g["category"], g.get("color"), g.get("material"),
                               g["temp_c"][0], g["temp_c"][1], dumps(g["occasions"]), g.get("like", 3),
                               "initial_wardrobe", now))
        kernel.instant["change_clothes"] = self._change
        kernel.instant["pick_shelf_garment"] = self._pick_shelf

    def _granted_garment_id(self, actor_id, raw_id, spec):
        owners = spec.get("owners") or ["*"]
        if owners == [actor_id] or raw_id.startswith(actor_id + "_") or raw_id.startswith(actor_id + "-"):
            return raw_id
        return f"{actor_id}__{raw_id}"

    # ------------------------------------------------------------------ facts
    def add(self, db, actor, garment, source):
        """A new garment from a purchase or a gift; source is the fact id (event:<id>)."""
        db.execute("INSERT OR IGNORE INTO garments VALUES(?,?,?,?,?,?,?,?,?,?,?,'clean',?)",
                   (garment["id"], actor, garment["label"], garment["category"], garment.get("color"),
                    garment.get("material"), garment["temp_c"][0], garment["temp_c"][1], dumps(garment["occasions"]),
                    garment.get("like", 3), source, self.s.clock.now()))

    def owned(self, actor):
        with self.s.read() as c:
            return [dict(r, occasions=json.loads(r["occasions"])) for r in
                    c.execute("SELECT * FROM garments WHERE actor=? ORDER BY garment_id", (actor,))]

    def current(self, actor):
        with self.s.read() as c:
            r = c.execute("SELECT * FROM outfits WHERE actor=?", (actor,)).fetchone()
            if not r:
                return None
            ids = json.loads(r["items"])
            labels = {g["garment_id"]: g["label"] for g in
                      c.execute("SELECT garment_id,label FROM garments WHERE garment_id IN (%s)" % ",".join("?" * len(ids)),
                                ids)} if ids else {}
            return {"items": ids, "labels": [labels.get(i, i) for i in ids], "occasion": r["occasion"],
                    "since": r["since"], "event_id": r["event_id"]}

    def shelf_picks(self, actor, limit=12):
        """Active shelf items in 未分类/public shelf not yet claimed by this actor."""
        if not self.shelf_assets:
            return []
        with self.s.read() as c:
            claimed_assets = {
                r["asset_id"] for r in c.execute("SELECT DISTINCT asset_id FROM garment_asset_links WHERE actor=?", (actor,))
            }
            owned_ids = {r["garment_id"] for r in c.execute("SELECT garment_id FROM garments WHERE actor=?", (actor,))}
        out = []
        for aid, spec in self.shelf_assets.items():
            if spec.get("status", "active") != "active":
                continue
            owners = spec.get("owners") or ["*"]
            if "*" not in owners and actor not in owners:
                continue
            if aid in claimed_assets:
                continue
            grants = spec.get("grants") or []
            resolved = [(self._granted_garment_id(actor, g["id"], spec), g) for g in grants]
            if not resolved or any(gid in owned_ids for gid, _ in resolved):
                continue
            out.append({
                "asset_id": aid,
                "kind": spec.get("kind", "single"),
                "shelf": spec.get("shelf", "未分类"),
                "label": spec.get("label", aid),
                "grants": [{"id": gid, "label": g["label"], "cat": g["category"], "occ": g["occasions"]}
                           for gid, g in resolved],
            })
            if len(out) >= limit:
                break
        return out

    def wardrobe_choices(self, actor, max_items=24):
        """Bounded clean/worn inventory for life_context, guaranteeing >=1 valid BODY_COVER combo."""
        owned = [g for g in self.owned(actor) if g["state"] != "dirty"]
        if not owned:
            return []
        now = self.s.clock.now()
        with self.s.read() as c:
            recent = set()
            for r in c.execute("SELECT items FROM outfit_log WHERE actor=? AND at>?", (actor, now - 2 * 86400)):
                recent |= set(json.loads(r["items"]))
            cur_row = c.execute("SELECT items FROM outfits WHERE actor=?", (actor,)).fetchone()
            current_ids = set(json.loads(cur_row["items"])) if cur_row else set()
            linked = {
                r["garment_id"]: r["asset_id"]
                for r in c.execute("SELECT garment_id, asset_id FROM garment_asset_links WHERE actor=?", (actor,))
            }

        def priority(g):
            gid = g["garment_id"]
            return (
                1 if gid in linked else 0,
                1 if g["source"] != "initial_wardrobe" else 0,
                1 if gid in current_ids else 0,
                1 if gid not in recent else 0,
                g.get("liking") or 3,
                gid,
            )

        selected = {}
        by_cat = {}
        for g in sorted(owned, key=priority, reverse=True):
            by_cat.setdefault(g["category"], []).append(g)
        # Ensure at least one complete covering outfit exists in the candidate slice.
        for cover in BODY_COVER:
            if all(cat in by_cat for cat in cover):
                for cat in cover:
                    chosen = by_cat[cat][0]
                    selected[chosen["garment_id"]] = chosen
                break
        # Next cover each occasion & category diversity, then fill remaining capacity by priority.
        for occ in ("home", "out", "formal", "sleep", "sport", "onsen"):
            if len(selected) >= max_items:
                break
            for g in sorted(owned, key=priority, reverse=True):
                if occ in g["occasions"] and g["garment_id"] not in selected:
                    selected[g["garment_id"]] = g
                    break
        for g in sorted(owned, key=priority, reverse=True):
            if len(selected) >= max_items:
                break
            selected.setdefault(g["garment_id"], g)

        out = []
        for g in sorted(selected.values(), key=lambda x: (x["category"], x["garment_id"])):
            entry = {
                "id": g["garment_id"],
                "label": g["label"],
                "cat": g["category"],
                "occ": g["occasions"],
                "temp": [g["temp_lo"], g["temp_hi"]],
                "state": g["state"],
            }
            if g["garment_id"] in linked:
                entry["asset"] = linked[g["garment_id"]]
            out.append(entry)
        return out

    # ------------------------------------------------------------------ the action
    def _pick_shelf(self, k, db, action_id, actor, args, cmd):
        actor_id = actor["actor_id"]
        asset_id = args.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise Refused("bad_asset_id")
        spec = self.shelf_assets.get(asset_id)
        if not isinstance(spec, dict):
            raise Refused("unknown_shelf_asset")
        if spec.get("status", "active") != "active":
            raise Refused("shelf_asset_not_active")
        owners = spec.get("owners") or ["*"]
        if "*" not in owners and actor_id not in owners:
            raise Refused("not_for_this_actor")
        grants = spec.get("grants") or []
        if not grants:
            raise Refused("empty_shelf_asset")
        if db.execute(
            "SELECT 1 FROM garment_asset_links WHERE actor=? AND asset_id=?", (actor_id, asset_id)
        ).fetchone():
            raise Refused("already_owned")
        resolved = [(self._granted_garment_id(actor_id, g["id"], spec), g) for g in grants]
        gids = [gid for gid, _ in resolved]
        existing = db.execute(
            "SELECT garment_id FROM garments WHERE garment_id IN (%s)" % ",".join("?" * len(gids)), gids
        ).fetchone()
        if existing:
            raise Refused("already_owned")

        blob_ref, asset_sha = None, spec.get("sha256")
        img_path = resolve_asset_image_path(spec, self.asset_root)
        if self.blob_root and img_path and os.path.isfile(img_path):
            try:
                blob_ref, asset_sha = freeze_file(img_path, self.blob_root, expected_sha256=spec.get("sha256"))
            except BlobIntegrityError:
                raise Refused("invalid_asset_blob")

        now = self.s.clock.now()
        shelf_name = spec.get("shelf") or "未分类"
        eid = k._emit(
            "picked_shelf_garment",
            actor_id,
            action_id,
            actor["location"],
            {"asset_id": asset_id, "kind": spec.get("kind", "single"), "shelf": shelf_name,
             "garment_ids": gids, "label": spec.get("label", asset_id)},
            scope=f"room:{actor['location']}",
            occurred=now,
        )
        for idx, (gid, g) in enumerate(resolved):
            garment = dict(g, id=gid)
            self.add(db, actor_id, garment, f"event:{eid}")
            part = g.get("asset_part") or f"{asset_id}#{idx}"
            db.execute(
                "INSERT INTO garment_asset_links VALUES(?,?,?,?,?,?,?,?,?)",
                (gid, actor_id, asset_id, part, asset_sha, blob_ref, shelf_name, now, eid),
            )
        k._finish(db, action_id, "succeeded", None, now)
        return {"status": "succeeded", "action_id": action_id, "event_id": eid, "garment_ids": gids}

    def _change(self, k, db, action_id, actor, args, cmd):
        items, occasion = args.get("items"), args.get("occasion")
        if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
            raise Refused("bad_items")
        rows = {r["garment_id"]: r for r in db.execute(
            "SELECT * FROM garments WHERE garment_id IN (%s)" % ",".join("?" * len(items)), items)}
        if len(rows) != len(set(items)) or any(r["actor"] != actor["actor_id"] for r in rows.values()):
            raise Refused("not_her_garment")
        cats = [r["category"] for r in rows.values()]
        if any(cats.count(c) > 1 for c in SINGLE):
            raise Refused("two_of_one_kind")
        if not any(all(c in cats for c in cover) for cover in BODY_COVER):
            raise Refused("not_dressed")
        if any(r["state"] == "dirty" for r in rows.values()):
            raise Refused("garment_dirty")
        now = self.s.clock.now()
        eid = k._emit("changed_clothes", actor["actor_id"], action_id, actor["location"],
                      {"items": items, "occasion": occasion}, scope=f"room:{actor['location']}", occurred=now)
        db.execute("INSERT OR REPLACE INTO outfits VALUES(?,?,?,?,?)", (actor["actor_id"], dumps(items), occasion, now, eid))
        db.execute("INSERT INTO outfit_log VALUES(?,?,?,?,?)", (actor["actor_id"], dumps(items), occasion, now, eid))
        for i in items:
            if rows[i]["category"] not in ("accessory",):
                db.execute("UPDATE garments SET state='worn' WHERE garment_id=? AND state='clean'", (i,))
        actor_id = actor["actor_id"]
        origin = (cmd or {}).get("origin") or "routine"
        links = {
            r["garment_id"]: dict(r)
            for r in db.execute(
                "SELECT * FROM garment_asset_links WHERE garment_id IN (%s)" % ",".join("?" * len(items)), items
            )
        }
        eligible, skip_reason, sel_hash, snap_json = build_swap_snapshot(
            actor=actor_id,
            items=items,
            garment_rows={gid: dict(r) for gid, r in rows.items()},
            links_by_gid=links,
            shelf_assets=self.shelf_assets,
            occasion=occasion,
            origin=origin,
            render_actors=self.render_actors,
            blob_root=self.blob_root,
            profiles_dir=self.profiles_dir,
            asset_root=self.asset_root,
            require_display_look=self.require_display_look,
            require_garment_assets=self.require_garment_assets,
        )
        db.execute(
            "INSERT INTO wardrobe_render_outbox("
            "event_id, actor, outfit_key, items_json, occasion, origin, render_eligible, skip_reason, "
            "selection_hash, snapshot_json, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (eid, actor_id, f"{actor_id}|{outfit_id(items)}", dumps(items), occasion, origin,
             1 if eligible else 0, skip_reason, sel_hash, snap_json, now),
        )
        k._finish(db, action_id, "succeeded", None, now)
        return {"status": "succeeded", "action_id": action_id, "event_id": eid}

    # ------------------------------------------------------------------ L0 habit
    def pick(self, actor, occasion, temp_c, raining=False):
        """Her own L0 choice: a covering set suited to the occasion and weather, liked, not worn lately."""
        now = self.s.clock.now()
        with self.s.read() as c:
            recent = set()
            for r in c.execute("SELECT items FROM outfit_log WHERE actor=? AND at>?", (actor, now - 2 * 86400)):
                recent |= set(json.loads(r["items"]))
        pool = [g for g in self.owned(actor) if occasion in g["occasions"] and g["state"] != "dirty"]

        def score(g):
            fit = 0 if g["temp_lo"] <= temp_c <= g["temp_hi"] else -3
            return (fit, -1 if g["garment_id"] in recent else 0, g["liking"], g["garment_id"])

        best = {}
        for g in sorted(pool, key=score, reverse=True):
            best.setdefault(g["category"], g)
        for cover in BODY_COVER:
            if all(c in best for c in cover):
                chosen = [best[c]["garment_id"] for c in cover]
                break
        else:
            return None
        if occasion in ("out", "formal"):
            if "shoes" in best:
                chosen.append(best["shoes"]["garment_id"])
            if "outer" in best and (temp_c < 15 or raining):
                chosen.append(best["outer"]["garment_id"])
        if "accessory" in best and best["accessory"]["liking"] >= 4:
            chosen.append(best["accessory"]["garment_id"])
        return chosen

    def dress(self, actor, occasion, temp_c, raining=False):
        """Submit her change of clothes if what she wears does not fit the occasion. Returns the result or None."""
        cur = self.current(actor)
        if cur and cur["occasion"] == occasion:
            return None
        items = self.pick(actor, occasion, temp_c, raining)
        if not items:
            return None
        now = self.s.clock.now()
        return self.k.submit({"idempotency_key": f"dress:{actor}:{occasion}:{int(now // 60)}", "principal": "routine",
                              "actor_id": actor, "origin": "routine", "action_type": "change_clothes",
                              "args": {"items": items, "occasion": occasion}})

    def laundry_done(self, db, actor):
        """Washing (her laundry activity finished): her worn clothes are clean again."""
        db.execute("UPDATE garments SET state='clean' WHERE actor=? AND state IN ('worn','dirty')", (actor,))
