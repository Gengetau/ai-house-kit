"""The kit's own addition to the shared promise file: the switch `INTENTS_PRESENCE` (see UPSTREAM.md).

The shared file already uses the name `PRESENCE` for the table definitions that `presence_from_chat` runs, so the
switch lives in `PRESENCE_ON`. These cases load a fresh copy under each value of the environment variable: they
fail if the switch is not read from the environment, or if the two names are folded into one again."""
import os
import unittest
from unittest.mock import patch

import _copy  # noqa: F401  (path and defaults)
import roster
from ai_house.common import loader


def fresh(value):
    env = dict(os.environ)
    env.pop("INTENTS_PRESENCE", None)
    if value is not None:
        env["INTENTS_PRESENCE"] = value
    with patch.dict(os.environ, env, clear=True):
        return loader.part(loader.load(roster=roster), "intents")


class PresenceSwitchTests(unittest.TestCase):
    def test_the_environment_decides_and_the_default_is_on(self):
        self.assertIs(fresh(None).PRESENCE_ON, True)
        self.assertIs(fresh("1").PRESENCE_ON, True)
        self.assertIs(fresh("0").PRESENCE_ON, False)

    def test_the_table_definitions_keep_their_name(self):
        for value in ("0", "1"):
            ddl = fresh(value).PRESENCE
            self.assertIsInstance(ddl, tuple)
            self.assertTrue(all(str(d).startswith("create ") for d in ddl))

    def test_off_reads_no_room_out_of_the_owners_words(self):
        """With the switch off the function answers None before touching any table; on, the same call needs the
        world database (here absent on purpose), which is how we know the early return is what answered."""
        off = fresh("0")
        with patch.object(off.W, "db", side_effect=AssertionError("the world database was opened")):
            self.assertIsNone(off.presence_from_chat([("owner", "我在厨房")], "private:1", 1000.0, [1000.0],
                                                     reader="nell", ids=[1]))
        on = fresh("1")
        self.assertIsNotNone(on.presence_of("我在厨房"))  # the sentence does name a room
        with patch.object(on.W, "db", side_effect=AssertionError("the world database was opened")):
            with self.assertRaises(AssertionError):
                on.presence_from_chat([("owner", "我在厨房")], "private:1", 1000.0, [1000.0], reader="nell", ids=[1])


if __name__ == "__main__":
    unittest.main()
