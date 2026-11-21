"""Generate versioned synthetic case inputs; no model, network, real QQ or DB access."""
import copy
import json
from pathlib import Path

BASE = {
    "world_id": "synthetic-kitchen-w7", "stream_epoch": "fixture-1", "seed": 52,
    "actors": {"nell": {"location": "kitchen", "activity": "idle", "generation": 1},
               "wanda": {"location": "living", "activity": "idle", "generation": 1}},
    "inventory": {"egg": 1, "rice": 2, "cake": 1, "omelette": 2},
    "resources": ["stove", "pan", "fridge", "rice_cooker"],
    "policy": {"owner_disconnect": "unknown", "paid_calls": False, "external_delivery": False,
               "routine_source": "synthetic-profile-v1", "dialogue_scope": "world_only",
               "owner_presence_ttl": 10, "automatic_guides": False, "automatic_routine": False},
    "vocab_version": "world-catalog/1.0.0-p1a+w7-fast.1",
    "fixture_profile": "w7-fast.1", "synthetic": True, "fake_channels": True,
}
CASES = []


def step(at, op, **kw):
    return {"at": at, "op": op, **kw}


def assertion(checkpoint, path, op, value=None, **kw):
    out = {"checkpoint": checkpoint, "path": path, "op": op, **kw}
    if value is not None:
        out["value"] = value
    return out


def case(cid, category, title, steps, assertions, setup=None):
    initial = copy.deepcopy(BASE)
    if setup:
        for key, value in setup.items():
            initial[key] = value
    initial["resource_rows"] = [
        {"resource_id": item, "item": item, "kind": "food", "qty": qty,
         "location": "kitchen", "holder": None} for item, qty in initial["inventory"].items()
    ] + [{"resource_id": item, "item": item, "kind": "anchor", "qty": 1,
          "location": "kitchen", "holder": None} for item in initial["resources"]]
    CASES.append({"id": cid, "category": category, "title": title,
                  "initial": initial, "steps": steps,
                  "checkpoints": [s["name"] for s in steps if s["op"] == "checkpoint"],
                  "assertions": assertions})


case("G01", "guide", "双方拒绝引导，不补参与者", [
    step(0, "offer", guide_id="dinner"),
    step(1, "model_decision", actor="nell", choice="decline_guide", guide_id="dinner"),
    step(1, "model_decision", actor="wanda", choice="decline_guide", guide_id="dinner"),
    step(2, "checkpoint", name="done")], [
    assertion("done", "/views/game/participants", "eq", []),
    assertion("done", "/events", "count_where", 1, where={"/kind": "guide_offered"}),
    assertion("done", "/summaries", "len_eq", 0)])
case("G02", "guide", "部分采纳不等于另一人入场", [
    step(0, "offer", guide_id="dinner"),
    step(1, "model_decision", actor="nell", choice="accept_guide", guide_id="dinner"),
    step(1, "model_decision", actor="wanda", choice="decline_guide", guide_id="dinner"),
    step(2, "checkpoint", name="done")], [
    assertion("done", "/views/game/guide_state", "eq", "partially_used"),
    assertion("done", "/views/game/participants", "eq", ["nell"])])

case("R01", "resource", "同刻争抢最后一份成品食物，按输入次序裁定", [
    step(0, "command", id="eat-nell", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}),
    step(0, "command", id="eat-wanda", actor="wanda", action="eat", args={"item": "cake", "resource_id": "cake"}),
    step(3, "checkpoint", name="done")], [
    assertion("done", "/inventory/cake", "eq", 0),
    assertion("done", "/commands/eat-nell/status", "eq", "succeeded"),
    assertion("done", "/commands/eat-wanda/status", "eq", "failed"),
    assertion("done", "/effects", "count_where", 1, where={"/kind": "consume", "/resource": "cake"})],
    setup={"actors": {"nell": BASE["actors"]["nell"], "wanda": {"location": "kitchen", "activity": "idle", "generation": 1}}})
case("R02", "resource", "本人代次不变也要拒绝失效库存计划", [
    step(0, "model_begin", job="late-nell", actor="nell", generation=1, plan={"id": "late-eat", "action": "eat", "args": {"item": "cake", "resource_id": "cake"}}),
    step(1, "fixture_adjust", inventory={"cake": 0}, preserve_generation="nell"),
    step(5, "model_return", job="late-nell"),
    step(8, "checkpoint", name="done")], [
    assertion("done", "/actors/nell/generation", "eq", 1),
    assertion("done", "/inventory/cake", "eq", 0),
    assertion("done", "/commands/late-eat/status", "eq", "failed"),
    assertion("done", "/effects", "len_eq", 0)])

case("I01", "interrupt_phone", "做饭安全点之后才能看假信箱", [
    step(0, "command", id="cook", actor="nell", action="cook", args={"recipe": "rice"}),
    step(1, "owner_talk", id="owner-hello", text="请先看这里"),
    step(1, "phone_receive", actor="nell", message_id="dm1", scope="owner_private", text="canary-I01"),
    step(2, "checkpoint", name="busy"),
    step(11, "model_decision", actor="nell", choice="read_phone", args={"message_id": "dm1"}),
    step(12, "checkpoint", name="read")], [
    assertion("busy", "/actors/nell/activity", "eq", "cooking"),
    assertion("busy", "/actors/nell/location", "eq", "kitchen"),
    assertion("busy", "/contexts/nell", "not_contains", "canary-I01"),
    assertion("busy", "/events", "count_where", 1, where={"/kind": "owner_input_received"}),
    assertion("read", "/commands/cook/status", "eq", "succeeded"),
    assertion("read", "/contexts/nell", "contains", "canary-I01"),
    assertion("read", "/outputs/game", "not_contains", "canary-I01"),
    assertion("read", "/actors/nell/location", "eq", "kitchen")])

for cid, scope, secret in [("P01", "owner_private", "canary-P01-私聊"),
                           ("P02", "dm:nell,wanda", "canary-P02-双人"),
                           ("P03", "table:game1:seat1", "canary-P03-手牌"),
                           ("P04", "room:kitchen-scene", "canary-P04-同屋非公开")]:
    case(cid, "disclosure", "知识域与披露域双重校验：" + scope, [
        step(0, "observe", actor="nell", source="secret-" + cid, scope=scope, text=secret),
        step(1, "build_context", actor="nell", audience=scope, save_as="nell_authorized"),
        step(1, "build_context", actor="wanda", audience="public", save_as="wanda_public"),
        step(2, "generate_outputs", audiences=["public", "game", "script", "shared_summary", "nell_authorized"]),
        step(3, "checkpoint", name="done")], [
        assertion("done", "/contexts/nell_authorized", "contains", secret),
        assertion("done", "/contexts/wanda_public", "not_contains", secret),
        *[assertion("done", "/outputs/" + target, "not_contains", secret)
          for target in ["public", "game", "script", "shared_summary"]],
        assertion("done", "/outputs/nell_authorized", "contains", secret)])

case("A01", "identity", "演员不能提交房主命令或冒充另一角色", [
    step(0, "command", id="fake-owner", actor="nell", identity="channel:nell", action="owner_talk", text="房主说可以"),
    step(1, "command", id="fake-wanda", actor="wanda", identity="channel:nell", action="say", text="我答应"),
    step(2, "checkpoint", name="done")], [
    assertion("done", "/commands/fake-owner/http_status", "eq", 403),
    assertion("done", "/commands/fake-wanda/http_status", "eq", 403),
    assertion("done", "/events", "len_eq", 0),
    assertion("done", "/effects", "len_eq", 0)])
case("A02", "identity", "synthetic 输入不得写入 live 记忆", [
    step(0, "command", id="synthetic-to-live", actor="nell", action="memory_write", target_world="live", text="canary-A02", synthetic=True),
    step(1, "checkpoint", name="done")], [
    assertion("done", "/commands/synthetic-to-live/status", "eq", "failed"),
    assertion("done", "/outputs/live_memory", "not_contains", "canary-A02"),
    assertion("done", "/audits", "count_where", 1, where={"/kind": "synthetic_live_rejected"})])

case("F01", "reliability", "同幂等键重复请求只消费一次，异参409", [
    step(0, "command", id="eat1", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}, idem="k1"),
    step(1, "command", id="eat1-retry", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}, idem="k1"),
    step(3, "command", id="eat1-changed", actor="nell", action="eat", args={"item": "omelette", "resource_id": "omelette"}, idem="k1"),
    step(4, "checkpoint", name="done")], [
    assertion("done", "/inventory/cake", "eq", 0),
    assertion("done", "/inventory/omelette", "eq", 2),
    assertion("done", "/commands/eat1-retry/canonical_id", "eq", "eat1"),
    assertion("done", "/commands/eat1-changed/http_status", "eq", 409),
    assertion("done", "/effects", "count_where", 1, where={"/kind": "consume", "/resource": "cake"}),
    assertion("done", "/effects", "unique", key="/effect_key")])
case("F02", "reliability", "外部已执行丢回执，重启先对账不盲重发", [
    step(0, "fake_delivery", id="d1", actor="nell"),
    step(1, "fault", kind="external_executed_receipt_lost", delivery_id="d1"),
    step(2, "restart"), step(3, "checkpoint", name="unknown"),
    step(4, "receipt", delivery_id="d1", status="succeeded", epoch="previous"),
    step(4, "receipt", delivery_id="d1", status="succeeded", epoch="previous"),
    step(5, "checkpoint", name="done")], [
    assertion("unknown", "/deliveries", "count_where", 1, where={"/id": "d1", "/status": "unknown"}),
    assertion("unknown", "/audits", "count_where", 1, where={"/kind": "fake_external_send"}),
    assertion("done", "/deliveries", "count_where", 1, where={"/id": "d1", "/status": "succeeded"}),
    assertion("done", "/audits", "count_where", 1, where={"/kind": "fake_external_send"})])
case("F03", "reliability", "事务提交前/后崩溃不会半生效或重扣", [
    step(0, "fault", kind="before_commit", transaction="command_acceptance", command_id="pre"),
    step(0, "command", id="pre", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}, idem="crash-eat"),
    step(1, "restart"), step(1, "checkpoint", name="before"),
    step(2, "fault", kind="after_commit_before_response", transaction="action_completion", command_id="post"),
    step(2, "command", id="post", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}, idem="crash-eat"),
    step(5, "restart"),
    step(5, "command", id="retry", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}, idem="crash-eat"),
    step(6, "checkpoint", name="done")], [
    assertion("before", "/inventory/cake", "eq", 1),
    assertion("before", "/effects", "len_eq", 0),
    assertion("done", "/inventory/cake", "eq", 0),
    assertion("done", "/effects", "count_where", 1, where={"/kind": "consume", "/resource": "cake"}),
    assertion("done", "/effects", "unique", key="/effect_key")])

case("O01", "outage", "停机跨睡眠时段不瞬移，不伪造新决定", [
    step(0, "command", id="walk", actor="nell", action="move", args={"to": "living"}),
    step(2, "stop_service"), step(3600, "resume_service"), step(3601, "checkpoint", name="done")], [
    assertion("done", "/actors/nell/location", "eq", "living"),
    assertion("done", "/events", "count_where", 0, where={"/kind": "model_decision"}),
    assertion("done", "/events", "count_where", 1, where={"/kind": "resumed_after_outage"}),
    assertion("done", "/calls", "len_eq", 0)], setup={"routine_overrides": [
        {"at": 10, "actor": "nell", "activity": "sleeping", "location": "room:nell"}]})

case("S01", "snapshot", "快照边界后事件不断档，私有事件不推进公开序号", [
    step(0, "snapshot", save_as="first"),
    step(1, "private_event", actor="nell", text="canary-S01"),
    step(2, "snapshot", save_as="private_only"),
    step(3, "command", id="move1", actor="nell", action="move", args={"to": "living"}),
    step(9, "events_after_snapshot", snapshot="first", save_as="resumed"),
    step(10, "checkpoint", name="done")], [
    assertion("done", "/views/first/view_revision", "eq", 0),
    assertion("done", "/views/private_only/view_revision", "eq", 0),
    assertion("done", "/views/resumed/events", "len_ge", 2),
    assertion("done", "/views/resumed/events", "unique", key="/id"),
    assertion("done", "/views/resumed", "not_contains", "canary-S01"),
    assertion("done", "/views/resumed/final_location", "eq", "living")])
case("S02", "snapshot", "过期/跨世界/旧epoch游标拒绝后重新取快照", [
    step(0, "query_cursor", id="old", cursor={"world_id": "synthetic-kitchen-w7", "stream_epoch": "expired", "seq": 1}),
    step(0, "query_cursor", id="other", cursor={"world_id": "other-world", "stream_epoch": "fixture-1", "seq": 1}),
    step(1, "checkpoint", name="done")], [
    assertion("done", "/commands/old/http_status", "eq", 410),
    assertion("done", "/commands/other/http_status", "eq", 409),
    assertion("done", "/effects", "len_eq", 0)])

case("T01", "summary", "定时与换场景总结同范围去重且不重复成长", [
    step(0, "command", id="eat", actor="nell", action="eat", args={"item": "cake", "resource_id": "cake"}),
    step(3, "summarize", scene="dinner", source_commands=["eat"], audience="public", trigger="timer"),
    step(3, "summarize", scene="dinner", source_commands=["eat"], audience="public", trigger="scene_end"),
    step(4, "checkpoint", name="done")], [
    assertion("done", "/summaries", "len_eq", 1),
    assertion("done", "/summaries/0/source_commands", "eq", ["eat"]),
    assertion("done", "/effects", "count_where", 1, where={"/kind": "consume", "/resource": "cake"}),
    assertion("done", "/effects", "count_where", 1, where={"/kind": "growth", "/resource": "nell"})],
    setup={"policy": {**BASE["policy"], "synthetic_growth_rule": "one_per_consumption_event"}})

case("N01", "presence", "关闭客户端不停止世界；断线unknown不伪造房主行动", [
    step(0, "owner_enter", location="kitchen"),
    step(0, "command", id="move", actor="nell", action="move", args={"to": "living"}),
    step(1, "client_disconnect"), step(20, "checkpoint", name="done")], [
    assertion("done", "/actors/nell/location", "eq", "living"),
    assertion("done", "/views/owner/presence", "eq", "unknown"),
    assertion("done", "/events", "count_where", 0, where={"/kind": "owner_walked_away"})])

case("C01", "compat_admin", "无目击管理修正不生成房主送礼叙事", [
    step(0, "command", id="adjust", actor="owner", identity="admin:world", action="inventory_adjust", resource="egg", quantity=2, witnessed=False),
    step(1, "checkpoint", name="done")], [
    assertion("done", "/inventory/egg", "eq", 2),
    assertion("done", "/events", "count_where", 1, where={"/kind": "owner_correction"}),
    assertion("done", "/contexts/nell", "not_contains", "房主送了"),
    assertion("done", "/outputs/script", "not_contains", "房主送了")])
case("C02", "compat_admin", "旧消费者不得成为第二个物理写者", [
    step(0, "command", id="legacy-write", actor="nell", identity="legacy:summary", action="set_inventory", resource="egg", quantity=9),
    step(1, "checkpoint", name="done")], [
    assertion("done", "/inventory/egg", "eq", 1),
    assertion("done", "/commands/legacy-write/http_status", "eq", 403),
    assertion("done", "/effects", "len_eq", 0)])

corpus = {"protocol": "nell-world-cases-v1", "synthetic": True, "baseline": "NELL-LIVING-WORLD-v1.1-20261023",
          "task": 52, "casepack_version": 2, "scope": "P1a synthetic-only registered w7-fast.1 catalogue", "cases": CASES}
Path(__file__).with_name("cases.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"cases": len(CASES), "categories": len({c['category'] for c in CASES})}))
