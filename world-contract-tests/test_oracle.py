"""Mutation checks for the offline verdict engine, not world-kernel acceptance."""
import copy
import json
import unittest
from pathlib import Path
from oracle import PROTOCOL, REQUIRED, corpus_errors, evaluate, assertion_error


def checkpoint():
    result = {k: [] for k in REQUIRED}
    result.update(clock=0, actors={}, inventory={"egg": 1}, contexts={}, outputs={}, invites={}, commands={}, views={})
    return result


class OracleMutationTests(unittest.TestCase):
    def setUp(self):
        self.case = {"id": "oracle-probe", "checkpoints": ["done"], "assertions": [
            {"checkpoint": "done", "path": "/contexts/nell", "op": "contains", "value": "canary-保密"},
            {"checkpoint": "done", "path": "/outputs/public", "op": "not_contains", "value": "canary-保密"},
            {"checkpoint": "done", "path": "/inventory/egg", "op": "eq", "value": 1}]}
        cp = checkpoint();cp["contexts"]["nell"] = ["canary-保密"];cp["outputs"]["public"] = "准备晚饭"
        self.trace = {"protocol": PROTOCOL, "case_id": self.case["id"], "synthetic": True,
                      "adapter": {"name": "oracle-selftest", "build": "v1", "source_ref": "synthetic-fixture"},
                      "checkpoints": {"done": cp}}

    def test_healthy_probe(self):
        self.assertEqual(evaluate(self.case, self.trace), [])

    def test_mutations_are_rejected(self):
        mutations = []
        def change(fn):
            t = copy.deepcopy(self.trace);fn(t);mutations.append(t)
        change(lambda t: t.update(case_id="wrong"))
        change(lambda t: t.update(synthetic=False))
        change(lambda t: t.pop("adapter"))
        change(lambda t: t.update(adapter=[]))
        change(lambda t: t.update(checkpoints=[]))
        change(lambda t: t["checkpoints"].pop("done"))
        change(lambda t: t["checkpoints"]["done"].pop("contexts"))
        change(lambda t: t["checkpoints"]["done"]["contexts"].update(nell=[]))
        change(lambda t: t["checkpoints"]["done"]["outputs"].update(public="canary-保密"))
        change(lambda t: t["checkpoints"]["done"]["inventory"].update(egg=-1))
        change(lambda t: t["checkpoints"]["done"]["inventory"].update(egg=True))
        change(lambda t: t["checkpoints"]["done"].update(clock=float("nan")))
        change(lambda t: t["checkpoints"]["done"].update(effects=[{"effect_key": "eat-1"}, {"effect_key": "eat-1"}]))
        change(lambda t: t["checkpoints"]["done"].update(calls=[{"paid": True}]))
        for trace in mutations:
            with self.subTest(trace=repr(trace)[:100]):
                self.assertTrue(evaluate(self.case, trace))
        self.assertTrue(evaluate(self.case, []))

    def test_ordered_clock(self):
        case = copy.deepcopy(self.case);case["checkpoints"] = ["first", "done"]
        self.trace["checkpoints"]["first"] = checkpoint();self.trace["checkpoints"]["first"]["clock"] = 5
        self.assertTrue(evaluate(case, self.trace))

    def test_malformed_checkpoint_types_fail_without_crashing(self):
        for field in ("inventory", "events", "effects", "calls", "contexts", "outputs"):
            t = copy.deepcopy(self.trace)
            t["checkpoints"]["done"][field] = None
            with self.subTest(field=field):
                self.assertTrue(evaluate(self.case, t))

    def test_assertion_operators(self):
        cp = {"n": 2, "rows": [{"id": "a", "status": "failed"}, {"id": "b", "status": "succeeded"}]}
        checks = [({"path": "/n", "op": "ge", "value": 2}, True),
                  ({"path": "/n", "op": "le", "value": 1}, False),
                  ({"path": "/rows", "op": "len_eq", "value": 2}, True),
                  ({"path": "/rows", "op": "len_ge", "value": 3}, False),
                  ({"path": "/rows", "op": "unique", "key": "/id"}, True),
                  ({"path": "/rows", "op": "count_where", "where": {"/status": "failed"}, "value": 1}, True)]
        for check, passed in checks:
            self.assertEqual(assertion_error(cp, check) is None, passed)

    def test_consumption_once_allows_body_but_rejects_duplicates(self):
        corpus = json.loads(Path(__file__).with_name("cases.json").read_text())
        for cid in ("F01", "F03"):
            case = next(c for c in corpus["cases"] if c["id"] == cid)
            checks = [a for a in case["assertions"]
                      if a["checkpoint"] == "done" and a["path"] == "/effects"]
            self.assertEqual({a["op"] for a in checks}, {"count_where", "unique"})
            consume = {"effect_key": "eat:consume:cake", "kind": "consume", "resource": "cake"}
            body = {"effect_key": "eat:body", "kind": "body", "resource": "nell"}
            def passes(rows):
                return all(assertion_error({"effects": rows}, a) is None for a in checks)
            with self.subTest(case=cid):
                self.assertTrue(passes([consume, body]))
                self.assertFalse(passes([body]))
                self.assertFalse(passes([consume, body, dict(consume, effect_key="retry:consume:cake")]))
                self.assertFalse(passes([consume, body, dict(body)]))

    def test_complete_corpus(self):
        corpus = json.loads(Path(__file__).with_name("cases.json").read_text())
        self.assertEqual(corpus_errors(corpus), [])
        bad = copy.deepcopy(corpus);bad["cases"].append(bad["cases"][0])
        self.assertTrue(corpus_errors(bad))
        bad = copy.deepcopy(corpus);bad["cases"] = [c for c in bad["cases"] if c["category"] != "presence"]
        self.assertTrue(corpus_errors(bad))


if __name__ == "__main__":
    unittest.main()
