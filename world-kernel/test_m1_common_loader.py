"""M1 step 1: the shared memory module is loaded as a private copy per caller (ai_house.common.loader).

The module keeps its settings and database path in module globals; two houses in one process must not share them.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from ai_house.common import loader, roster


def _house(names):
    return roster.make([{"actor": a, "qq": 1000 + i, "name": n} for i, (a, n) in enumerate(names.items())],
                       owner={"qq": 999, "aliases": ("主人",)})


class LoaderTests(unittest.TestCase):
    def load(self, r=None):
        m = loader.load(roster=r)
        self.addCleanup(loader.unload, m)
        return m

    def test_two_copies_share_no_settings_and_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self.load(_house({"nia": "妮娅"})), self.load(_house({"xingli": "星璃"}))
            self.assertIsNot(a, b)
            self.assertIsNot(a.ID, b.ID)
            a.DB_PATH, b.DB_PATH = str(Path(tmp) / "a.db"), str(Path(tmp) / "b.db")
            a.OWNER_CHATS, b.OWNER_CHATS = {"nia@private:999"}, {"xingli@private:999"}
            for m in (a, b):
                m.embed = lambda texts, timeout=20: []
                m.init()
            a.add("nia", "private:999", "房主说下周要交稿")
            self.assertEqual(len(a.search("nia", "private:999", "下周要交稿")), 1)
            self.assertEqual(b.search("xingli", "private:999", "下周要交稿"), [])
            self.assertEqual(b.search("nia", "private:999", "下周要交稿"), [])

    def test_each_copy_tells_people_by_its_own_roster(self):
        a, b = self.load(_house({"nia": "妮娅"})), self.load(_house({"xingli": "星璃"}))
        line = "[妮娅|1000] (message_id=1): 早"
        self.assertTrue(a.ID.is_self(line, "nia"))
        self.assertEqual(a.ID.who(1000), "nia")
        self.assertEqual(b.ID.who(1000), "xingli")  # the same number is someone else in the other house
        self.assertEqual(a.ID.who(999), "owner")
        self.assertEqual(a.ID.named("主人"), "owner")  # the owner's other names come from the roster
        self.assertEqual(a.NAMES, {"nia": "妮娅"})

    def test_without_a_roster_nobody_is_known(self):
        m = self.load()
        self.assertEqual(m.NAMES, {})
        self.assertIsNone(m.ID.who(12345))
        self.assertEqual(m.ID.named("房主"), "owner")

    def test_the_shipped_defaults_name_no_endpoint_and_exempt_no_group(self):
        m = self.load()
        for name in ("EMBED_URL", "CPA_BASE", "CPA_CONFIG", "MODEL"):
            self.assertEqual(getattr(m, name), "", name)
        self.assertEqual(m.LEAK_FREE_CHATS, set())
        self.assertIsNone(m.embed(["没有向量服务"]))  # keyword search only, no request made

    def test_unload_takes_the_copy_out_of_the_import_table(self):
        m = loader.load()
        name = m.__copy__
        self.assertIn(name + ".memory", sys.modules)
        loader.unload(m)
        self.assertEqual([k for k in sys.modules if k.startswith(name)], [])

    def test_a_roster_with_a_shared_or_missing_number_is_refused(self):
        with self.assertRaises(ValueError):
            roster.make([{"actor": "a", "qq": 5, "name": "甲"}, {"actor": "b", "qq": 5, "name": "乙"}], owner={"qq": 9})
        with self.assertRaises(ValueError):
            roster.make([{"actor": "a", "qq": 5, "name": "甲"}])  # no number for the owner


if __name__ == "__main__":
    unittest.main()
