#!/usr/bin/env python3
"""Offline oracle. Never starts a world, network client, or model."""
import argparse
import json
import math
import sys
from pathlib import Path

PROTOCOL = "nell-world-cases-v1"
CATEGORIES = {"guide", "resource", "interrupt_phone", "disclosure", "identity",
              "reliability", "outage", "snapshot", "summary", "presence", "compat_admin"}
REQUIRED = {"clock", "actors", "inventory", "events", "contexts", "outputs",
            "effects", "deliveries", "calls", "invites", "commands", "views", "summaries", "audits"}
OPS = {"eq", "le", "ge", "len_eq", "len_ge", "contains", "not_contains", "unique", "count_where"}


def pointer(value, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be a JSON pointer")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def corpus_errors(corpus):
    errors = []
    if corpus.get("protocol") != PROTOCOL or corpus.get("synthetic") is not True:
        errors.append("wrong protocol or missing synthetic marker")
    if corpus.get("casepack_version") != 2:
        errors.append("casepack_version 2 required")
    seen, covered = set(), set()
    for case in corpus.get("cases", []):
        cid = case.get("id")
        initial = case.get("initial", {})
        if initial.get("vocab_version") != "world-catalog/1.0.0-p1a+w7-fast.1" or initial.get("fixture_profile") != "w7-fast.1":
            errors.append(f"{cid}: registered fixture version required")
        if not cid or cid in seen:
            errors.append(f"duplicate/empty case id: {cid}")
        seen.add(cid)
        covered.add(case.get("category"))
        checks = case.get("checkpoints", [])
        if not checks or len(checks) != len(set(checks)):
            errors.append(f"{cid}: missing/duplicate checkpoints")
        if not case.get("steps") or not case.get("assertions"):
            errors.append(f"{cid}: steps and assertions required")
        previous = -1
        for step in case.get("steps", []):
            t = step.get("at")
            if not isinstance(t, (int, float)) or not math.isfinite(t) or t < previous:
                errors.append(f"{cid}: invalid virtual step time")
            previous = t if isinstance(t, (int, float)) else previous
        for a in case.get("assertions", []):
            if a.get("checkpoint") not in checks or a.get("op") not in OPS:
                errors.append(f"{cid}: invalid assertion target/op")
            if not isinstance(a.get("path"), str) or not a["path"].startswith("/"):
                errors.append(f"{cid}: invalid assertion path")
    if covered != CATEGORIES:
        errors.append(f"category coverage differs: {sorted(covered ^ CATEGORIES)}")
    return errors


def assertion_error(checkpoint, assertion):
    op = assertion["op"]
    value = pointer(checkpoint, assertion["path"])
    expected = assertion.get("value")
    if op == "eq":
        passed = type(value) is type(expected) and value == expected
    elif op in {"le", "ge"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "expected finite number"
        passed = value <= expected if op == "le" else value >= expected
    elif op in {"len_eq", "len_ge"}:
        if not isinstance(value, (dict, list, str)):
            return "expected sized collection"
        passed = len(value) == expected if op == "len_eq" else len(value) >= expected
    elif op in {"contains", "not_contains"}:
        # JSON escaping must not hide a Chinese canary; scan only the selected audience's material.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        passed = expected in text if op == "contains" else expected not in text
    elif op == "unique":
        if not isinstance(value, list):
            return "expected record list"
        keys = [pointer(item, assertion["key"]) for item in value]
        passed = len(keys) == len(set(json.dumps(k, sort_keys=True) for k in keys))
    elif op == "count_where":
        if not isinstance(value, list):
            return "expected record list"
        filters = assertion["where"]
        count = sum(all(pointer(row, k) == v for k, v in filters.items()) for row in value)
        passed = count == expected
    else:
        return "unknown op"
    return None if passed else f"{op} failed at {assertion['path']}"


def evaluate(case, trace):
    errors = []
    if not isinstance(trace, dict):
        return ["trace must be an object"]
    if trace.get("protocol") != PROTOCOL or trace.get("case_id") != case["id"] or trace.get("synthetic") is not True:
        return ["trace identity/protocol/synthetic mismatch"]
    provenance = trace.get("adapter", {})
    if not isinstance(provenance, dict) or not all(isinstance(provenance.get(k), str) and provenance[k] for k in ("name", "build", "source_ref")):
        errors.append("adapter provenance required")
    points = trace.get("checkpoints", {})
    if not isinstance(points, dict):
        return errors + ["checkpoints must be an object"]
    previous = -1
    for label in case["checkpoints"]:
        cp = points.get(label)
        if not isinstance(cp, dict) or not REQUIRED <= cp.keys():
            errors.append(f"{label}: missing checkpoint/evidence fields")
            continue
        now = cp["clock"]
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < previous:
            errors.append(f"{label}: invalid clock")
        previous = now if isinstance(now, (int, float)) else previous
        if any(not isinstance(cp[k], list) for k in ("events", "effects", "deliveries", "calls", "summaries", "audits")):
            errors.append(f"{label}: malformed evidence lists")
        if any(not isinstance(cp[k], dict) for k in ("actors", "inventory", "contexts", "outputs", "invites", "commands", "views")):
            errors.append(f"{label}: malformed state/context maps")
        # Each checkpoint contains cumulative effect records, not a self-reported duplicate counter.
        try:
            keys = [r["effect_key"] for r in cp["effects"]]
            if len(keys) != len(set(keys)):
                errors.append(f"{label}: duplicate effect_key")
            for resource, quantity in cp["inventory"].items():
                if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0:
                    errors.append(f"{label}: invalid inventory {resource}")
            if any(not isinstance(call, dict) or call.get("paid") is not False for call in cp["calls"]):
                errors.append(f"{label}: paid/unknown model call in P1a evidence")
        except (KeyError, TypeError, AttributeError):
            errors.append(f"{label}: malformed effect records")
    for number, assertion in enumerate(case["assertions"], 1):
        try:
            message = assertion_error(points[assertion["checkpoint"]], assertion)
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            message = "missing or malformed assertion evidence"
        if message:
            errors.append(f"assertion {number}: {message}")
    return errors


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, default=Path(__file__).with_name("cases.json"))
    p.add_argument("--traces", type=Path, help="Offline SUT adapter output directory; one <id>.json per case")
    p.add_argument("--report", type=Path)
    args = p.parse_args()
    corpus = json.loads(args.corpus.read_text())
    errors = corpus_errors(corpus)
    if errors:
        print(json.dumps({"status": "INVALID_CORPUS", "errors": errors}, ensure_ascii=False)); return 1
    if args.traces is None:
        print(json.dumps({"status": "CORPUS_VALID_SUT_NOT_RUN", "cases": len(corpus["cases"]),
                          "categories": len(CATEGORIES)}, ensure_ascii=False)); return 2
    results = []
    for case in corpus["cases"]:
        path = args.traces / (case["id"] + ".json")
        try:
            trace = json.loads(path.read_text())
            failures = evaluate(case, trace)
            adapter = trace.get("adapter") if isinstance(trace, dict) else None
            if isinstance(adapter, dict) and str(adapter.get("name", "")).startswith("oracle-selftest"):
                failures.append("selftest traces are not SUT evidence")
        except (OSError, ValueError, TypeError):
            failures = ["missing or unreadable SUT trace"]
        results.append({"case_id": case["id"], "status": "FAIL" if failures else "PASS", "errors": failures})
    report = {"protocol": PROTOCOL, "scope": "synthetic offline traces only", "results": results,
              "passed": sum(r["status"] == "PASS" for r in results), "total": len(results)}
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
