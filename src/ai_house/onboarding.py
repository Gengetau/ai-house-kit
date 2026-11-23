"""Onboarding wizard, runtime custom WorldPack generator, and `.env` LLM persistence helpers."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from worldkernel import vocab_life1

ACTOR_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RESERVED_ACTORS = frozenset({
    "owner", "house", "shared", "public", "system", "admin", "all", "away",
    "living", "kitchen", "study", "bath", "yard",
})

SPRITE_OPTIONS: Dict[str, str] = {
    "sora": "料理围裙",
    "elsa": "书卷长发",
    "rin": "艺术画师",
    "kuro": "极客创客",
    "mina": "元气双马尾",
    "yuki": "安静短发",
    "alma": "休闲卫衣",
    "wanda": "森系长裙",
    "nell": "暖棕居家",
}

ACCENT_OPTIONS: Sequence[str] = (
    "#B4532D",
    "#0F766E",
    "#0284C7",
    "#DB2777",
    "#6F9068",
    "#7C3AED",
)

SCHEDULE_PRESETS: Dict[str, Dict[str, Any]] = {
    "early": {
        "label": "早睡早起",
        "weekday": {"wake": "06:30", "bedtime": "22:30"},
        "weekend": {"wake": "07:00", "bedtime": "22:30"},
        "night_owl": 0.15,
    },
    "regular": {
        "label": "规律作息",
        "weekday": {"wake": "07:30", "bedtime": "23:30"},
        "weekend": {"wake": "08:00", "bedtime": "23:30"},
        "night_owl": 0.35,
    },
    "night_owl": {
        "label": "夜猫创作",
        "weekday": {"wake": "09:30", "bedtime": "01:30"},
        "weekend": {"wake": "10:00", "bedtime": "02:00"},
        "night_owl": 0.75,
    },
}

COOK_PRESETS: Dict[str, Dict[str, Any]] = {
    "chef": {"label": "家常大厨", "skill": 3, "cook_priority": 1},
    "helper": {"label": "热心帮厨", "skill": 2, "cook_priority": 3},
    "foodie": {"label": "干饭食客", "skill": 1, "cook_priority": 6},
}

BASE_HOUSE: Dict[str, Any] = {
    "rooms": ["kitchen", "living", "study", "yard", "bath"],
    "routes": [
        {"from": "living", "to": "kitchen", "seconds": 8},
        {"from": "living", "to": "study", "seconds": 10},
        {"from": "living", "to": "yard", "seconds": 12},
        {"from": "living", "to": "bath", "seconds": 10},
        {"from": "living", "to": "bedrooms", "seconds": 15},
        {"from": "living", "to": "away", "seconds": 60},
    ],
    "anchors": {
        "stove": "kitchen",
        "pot": "kitchen",
        "pan": "kitchen",
        "rice_cooker": "kitchen",
        "fridge": "kitchen",
        "oven": "kitchen",
        "bathtub": "bath",
        "game_console": "living",
        "tv": "living",
        "board_games": "living",
        "piano": "living",
        "garden_bed": "yard",
        "desk": "study",
        "washing_machine": "bath",
    },
}

BASE_MONEY: Dict[str, int] = {
    "allowance_yen": 40000,
    "allowance_day": 1,
    "house_budget_yen": 200000,
}

DEFAULT_GOOD_AT = [
    "curry_rice", "tonjiru", "nikujaga", "chikuzenni", "miso_soup", "gyoza", "grilled_fish", "breakfast_wa",
]


def sprite_choices() -> List[Dict[str, str]]:
    return [{"id": k, "label": v} for k, v in SPRITE_OPTIONS.items()]


def suggest_actor_id(raw_actor: str = "", existing: Sequence[str] = ()) -> str:
    used = {str(x).strip().lower() for x in existing if str(x).strip()}
    cand = str(raw_actor or "").strip().lower()
    if cand:
        if not ACTOR_RE.fullmatch(cand):
            raise ValueError("英文代号需以小写字母开头，由 2-16 位小写字母、数字或下划线组成")
        if cand in RESERVED_ACTORS or cand.startswith("room_"):
            raise ValueError(f"英文代号 '{cand}' 为系统保留词，请换一个")
        if cand in used:
            raise ValueError(f"英文代号 '{cand}' 已被其他住客使用")
        return cand
    idx = 1
    while True:
        gen = f"bot{idx}"
        if gen not in used and gen not in RESERVED_ACTORS:
            return gen
        idx += 1


def _starter_wardrobe(actor: str) -> List[Dict[str, Any]]:
    return [
        {"id": f"{actor}_work_dress", "label": "米棕格子家居连衣裙套装", "category": "dress", "color": "米棕", "material": "棉", "temp_c": [12, 26], "occasions": ["home", "out"], "like": 5, "source": "自带"},
        {"id": f"{actor}_head_scarf", "label": "棉麻发带", "category": "accessory", "color": "米白", "material": "棉", "temp_c": [-30, 50], "occasions": ["home", "out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_apron_plain", "label": "做饭围裙", "category": "accessory", "color": "亚麻白", "material": "棉麻", "temp_c": [-30, 50], "occasions": ["home"], "like": 4, "source": "自带"},
        {"id": f"{actor}_blouse_shirt", "label": "白色亚麻衬衫", "category": "top", "color": "白", "material": "棉", "temp_c": [15, 28], "occasions": ["out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_skirt_navy", "label": "深蓝百褶中裙", "category": "bottom", "color": "藏青", "material": "涤棉", "temp_c": [12, 26], "occasions": ["out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_cardigan_warm", "label": "焦糖针织开衫", "category": "outer", "color": "焦糖", "material": "针织", "temp_c": [8, 20], "occasions": ["home", "out"], "like": 5, "source": "自带"},
        {"id": f"{actor}_onepiece_floral", "label": "浅蓝碎花长裙", "category": "dress", "color": "浅蓝", "material": "棉", "temp_c": [18, 30], "occasions": ["out"], "like": 3, "source": "自带"},
        {"id": f"{actor}_pajama_stripe", "label": "柔和条纹睡衣套装", "category": "sleepwear", "color": "米白", "material": "棉", "temp_c": [-30, 50], "occasions": ["sleep"], "like": 5, "source": "自带"},
        {"id": f"{actor}_lounge_set", "label": "宽松针织家居服套装", "category": "loungewear", "color": "燕麦色", "material": "针织棉", "temp_c": [-30, 50], "occasions": ["home"], "like": 5, "source": "自带"},
        {"id": f"{actor}_coat_winter", "label": "深灰羊毛大衣", "category": "outer", "color": "深灰", "material": "羊毛", "temp_c": [-5, 10], "occasions": ["out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_mary_janes", "label": "棕色低跟皮鞋", "category": "shoes", "color": "棕", "material": "皮", "temp_c": [-30, 50], "occasions": ["out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_sneakers", "label": "白色帆布运动鞋", "category": "shoes", "color": "白", "material": "网布", "temp_c": [-30, 50], "occasions": ["out", "sport"], "like": 4, "source": "自带"},
        {"id": f"{actor}_slippers", "label": "棉麻室内拖鞋", "category": "shoes", "color": "浅灰", "material": "棉", "temp_c": [-30, 50], "occasions": ["home"], "like": 4, "source": "自带"},
        {"id": f"{actor}_umbrella", "label": "晴雨两用折叠伞", "category": "accessory", "color": "浅蓝", "material": "化纤", "temp_c": [-30, 50], "occasions": ["out"], "like": 4, "source": "自带"},
        {"id": f"{actor}_tote_bag", "label": "帆布采买手提袋", "category": "accessory", "color": "原色帆布", "material": "帆布", "temp_c": [-30, 50], "occasions": ["out"], "like": 5, "source": "自带"},
        {"id": f"{actor}_yukata", "label": "夏日棉麻浴衣", "category": "yukata", "color": "浅紫", "material": "棉", "temp_c": [22, 32], "occasions": ["onsen", "out"], "like": 3, "source": "自带"},
        {"id": f"{actor}_swimsuit", "label": "藏青连体泳衣", "category": "swimwear", "color": "藏青", "material": "泳布", "temp_c": [24, 35], "occasions": ["sport"], "like": 2, "source": "自带"},
    ]


def build_person_doc(spec: Mapping[str, Any], existing_doc: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Validate and construct one WorldPack `people/<actor>.json` dictionary."""
    base = copy.deepcopy(dict(existing_doc)) if existing_doc else {}
    actor = str(base.get("actor") or spec.get("actor") or "").strip().lower()
    if not ACTOR_RE.fullmatch(actor) or actor in RESERVED_ACTORS or actor.startswith("room_"):
        raise ValueError("英文代号格式不合法或属于保留词")

    name = str(spec.get("name") if "name" in spec else base.get("name") or "").strip()
    if not (1 <= len(name) <= 24):
        raise ValueError("住客名字需在 1-24 个字符之间")

    bio_raw = spec.get("bio") if "bio" in spec else base.get("bio")
    bio = str(bio_raw or f"搬进 AI 小屋生活的 {name}。").strip()[:120]

    persona_raw = spec.get("persona") if "persona" in spec else base.get("persona")
    persona = str(persona_raw or "").strip()[:600]
    if not persona:
        persona = f"你是{name}（{actor}），住在 AI 小屋里的合租室友。{bio}"

    accent_raw = str(spec.get("accent") if "accent" in spec else base.get("accent") or "#B4532D").strip()
    accent = accent_raw if ACCENT_RE.fullmatch(accent_raw) else "#B4532D"

    sprite_raw = str(spec.get("sprite") if "sprite" in spec else base.get("sprite") or "sora").strip().lower()
    sprite = sprite_raw if sprite_raw in SPRITE_OPTIONS else "sora"

    sched_key = str(spec.get("schedule_preset") if "schedule_preset" in spec else base.get("schedule_preset") or "regular").strip()
    if sched_key not in SCHEDULE_PRESETS:
        sched_key = "regular"
    sched_tpl = SCHEDULE_PRESETS[sched_key]

    cook_key = str(spec.get("cook_preset") if "cook_preset" in spec else base.get("cook_preset") or "helper").strip()
    if cook_key not in COOK_PRESETS:
        cook_key = "helper"
    cook_tpl = COOK_PRESETS[cook_key]

    # Note: REGULAR_NOT_ACTIVITY in worldcatalog forbids `"cooking"` inside schedule.regular.
    default_regular = [
        {"days": "mon-fri", "at": "08:30-10:30", "activity": "housework", "where": "living"},
        {"days": "mon-fri", "at": "16:30-17:30", "activity": "shopping", "where": "place:supermarket"},
        {"days": "sat-sun", "at": "10:00-11:30", "activity": "reading", "where": "study"},
    ]
    existing_sched = copy.deepcopy(base.get("schedule") or {})
    if "schedule_preset" in spec or not existing_sched:
        schedule = {
            "weekday": dict(sched_tpl["weekday"]),
            "weekend": dict(sched_tpl["weekend"]),
            "regular": existing_sched.get("regular") or default_regular,
        }
    else:
        schedule = existing_sched

    existing_cooking = copy.deepcopy(base.get("cooking") or {})
    cooking = {
        "skill": int(cook_tpl["skill"] if ("cook_preset" in spec or not existing_cooking) else existing_cooking.get("skill", 2)),
        "good_at": list(existing_cooking.get("good_at") or DEFAULT_GOOD_AT),
        "cannot": list(existing_cooking.get("cannot") or []),
    }

    taste = copy.deepcopy(base.get("taste") or {
        "likes": ["black_tea", "miso_soup", "pudding_cup"],
        "dislikes": [],
        "never": [],
    })
    activities = copy.deepcopy(base.get("activities") or {
        "prefer": {"reading": 4, "tea_time": 4, "housework": 3},
        "avoid": [],
    })

    doc: Dict[str, Any] = {
        "actor": actor,
        "name": name,
        "bio": bio,
        "persona": persona,
        "accent": accent,
        "sprite": sprite,
        "schedule_preset": sched_key,
        "cook_preset": cook_key,
        "source": copy.deepcopy(base.get("source") or {
            "wardrobe": "onboarding",
            "cooking": "onboarding",
            "taste": "onboarding",
            "activities": "onboarding",
            "schedule": {"weekday": "onboarding", "weekend": "onboarding", "regular": "onboarding"},
            "wallet_start_yen": "onboarding",
        }),
        "wardrobe": copy.deepcopy(base.get("wardrobe") or _starter_wardrobe(actor)),
        "cooking": cooking,
        "taste": taste,
        "activities": activities,
        "schedule": schedule,
        "wallet_start_yen": int(base.get("wallet_start_yen") or BASE_MONEY["allowance_yen"]),
        "cook_priority": int(cook_tpl["cook_priority"] if ("cook_preset" in spec or "cook_priority" not in base) else base["cook_priority"]),
        "night_owl": float(sched_tpl["night_owl"] if ("schedule_preset" in spec or "night_owl" not in base) else base["night_owl"]),
    }
    return doc


def write_custom_pack(
    pack_dir: Path,
    people_docs: Sequence[Dict[str, Any]],
    *,
    profile: str = "custom-house",
    house: Optional[Mapping[str, Any]] = None,
    money: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Persist `manifest.json` + `people/*.json` into `pack_dir` and validate via `vocab_life1.load_pack`."""
    if not people_docs:
        raise ValueError("至少需要 1 位住客")
    pack_dir = Path(pack_dir)
    people_dir = pack_dir / "people"
    people_dir.mkdir(parents=True, exist_ok=True)

    roster = [str(d["actor"]) for d in people_docs]
    manifest = {
        "profile": profile,
        "vocab_version": "life1-0.5",
        "status": f"Custom AI House ({len(roster)} resident{'s' if len(roster) != 1 else ''})",
        "location_origin": "樱丘一带（开源自定义合租屋）",
        "travel_lookup_date": "2026-11-22",
        "files": ["ingredients", "recipes", "activities", "places", "clothing", "render"],
        "people": roster,
        "house": copy.deepcopy(dict(house)) if house else copy.deepcopy(BASE_HOUSE),
        "money": copy.deepcopy(dict(money)) if money else copy.deepcopy(BASE_MONEY),
    }

    for doc in people_docs:
        fp = people_dir / f"{doc['actor']}.json"
        fp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    (pack_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return vocab_life1.load_pack(pack_dir)


def copy_preset_to_custom_pack(preset: str, custom_pack_dir: Path) -> Any:
    """Instantiate a built-in sample WorldPack (`indie-studio-3` or `life1`) into `custom_pack_dir`."""
    clean = str(preset or "").strip()
    if clean not in ("indie-studio-3", "life1"):
        raise ValueError(f"不支持的预设世界包：{preset}")
    src_vocab = vocab_life1.load_pack("indie-studio-3" if clean == "indie-studio-3" else None)
    docs: List[Dict[str, Any]] = []
    for aid in src_vocab.ROSTER:
        doc = copy.deepcopy(dict(src_vocab.PEOPLE[aid]))
        doc["actor"] = aid
        doc.setdefault("name", src_vocab.NAMES.get(aid, aid))
        doc.setdefault("accent", "#0F766E")
        doc.setdefault("sprite", aid if aid in SPRITE_OPTIONS else "sora")
        docs.append(doc)
    return write_custom_pack(
        custom_pack_dir,
        docs,
        profile=str(src_vocab.MANIFEST.get("profile") or clean),
        house=src_vocab.HOUSE,
        money=src_vocab.MONEY,
    )


def _format_dotenv_value(val: str) -> str:
    clean = str(val or "").replace("\r", "").replace("\n", "").strip()
    if not clean:
        return ""
    if any(ch in clean for ch in (" ", "#", "'", '"')):
        escaped = clean.replace('"', '\\"')
        return f'"{escaped}"'
    return clean


def _write_in_place_private(real: Path, text: str) -> None:
    """Overwrite `real` in place (keeps inode, e.g. Docker single-file bind mounts) and force 0600."""
    fd = os.open(str(real), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(real, 0o600)
    except OSError:
        pass


def atomic_write_private_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text via a 0600 temp file, preserving symlinks.

    The real path of `path` is resolved first so a symlinked `.env` stays a link and the file it points to is
    updated. If `os.replace` fails (cross-device, EBUSY on a bind-mounted single file, ...), fall back to an
    in-place overwrite of the real file with 0600 permissions.
    """
    target = Path(os.path.realpath(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp, target)
    except OSError:
        try:
            _write_in_place_private(target, text)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def upsert_dotenv_vars(env_path: Path, updates: Mapping[str, str]) -> None:
    """Update or insert KEY=VALUE entries in `.env`, preserving unrelated comments and tokens."""
    env_path = Path(env_path)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else ["# AI House Studio environment"]
    pending = {str(k): _format_dotenv_value(v) for k, v in updates.items()}
    out_lines: List[str] = []
    seen = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        k = line.split("=", 1)[0].strip()
        if k in pending:
            out_lines.append(f"{k}={pending[k]}")
            seen.add(k)
        else:
            out_lines.append(line)

    for k, v in pending.items():
        if k not in seen:
            out_lines.append(f"{k}={v}")

    atomic_write_private_text(env_path, "\n".join(out_lines).rstrip() + "\n")


def mask_secret(val: str) -> str:
    """Mask a secret for display: short values (<20 chars) are fully hidden; longer ones show at most a
    vendor-style prefix such as `sk-` plus the last 4 characters."""
    clean = str(val or "").strip()
    if not clean:
        return ""
    if len(clean) < 20:
        return "••••••••"
    head = clean[:3] if clean[:3].endswith(("-", "_")) else ""
    return f"{head}…{clean[-4:]}"
