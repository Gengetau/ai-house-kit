"""Real-QQ run 1, step 7: in the listed (leak-free) group the owner asks "the plan I told you in private, do you remember?"
and the roommate must be handed the coffee-shop line (not the "you remember nothing" note).

Sequence: private line 1 (coffee shop, secret) -> private line 4 (tongue twister) -> group question. Memory table stays
empty, only `seen` rows exist (nothing extracted yet). No model, no network; every name and number is made up.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.memory import LEAK_FREE_GROUPS_ENV, PRIVATE_IN_GROUPS_ENV, MemoryStore  # noqa: E402

# the notes the shared module writes (they are no longer exported by ai_house.memory)
NO_MEMORY_NOTE = ("（你的记忆里没有找到和这个问题相关的内容。照实说你不记得、不知道，不要编、不要猜，"
                  "也不要替别人回答；除非这件事就出现在你们这次对话的前文里。）")
PRIVATE_IN_GROUP_NOTE_FREE = "标着·私密的是房主私聊里跟你说的事：这里是小屋自己的群，想提可以自然地提。"
PRIVATE_IN_GROUP_NOTE_HUSH = "标着·私密的是房主私聊里跟你说的事：心里知道就好，别在这里说出来，也别暗示。"

T0 = 1_800_000_000.0
LINE1 = "妮娅，悄悄跟你说个事：我打算下个月辞职去开一家咖啡店，这件事先别告诉别人哦。"
LINE4 = "对了，我最近在练绕口令，这句老念不顺：红鲤鱼与绿鲤鱼与驴，黑化肥发灰会挥发。"
LISTED = "qq-group:222222"
UNLISTED = "qq-group:111111"
ASKS = (
    "这里都是自己人。妮娅，我之前私下跟你说的那个打算，你还记得是什么吗？在这儿说没关系。",
    "妮娅，我之前私下跟你说的那个打算，你还记得是什么吗？",
)


class ListedGroupRecall(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.st = MemoryStore(
            tmp.name, names={"nia": "妮娅"}, env={PRIVATE_IN_GROUPS_ENV: "1", LEAK_FREE_GROUPS_ENV: LISTED}
        )
        self.addCleanup(self.st.close)
        for line, at in ((LINE1, T0), (LINE4, T0 + 600)):
            self.st.observe("nia", "private", line, channel="private:nia", speaker="房主", speaker_role="owner",
                            reply="好的", now=at)

    def test_listed_group_gets_the_coffee_shop_line_and_the_free_note(self) -> None:
        for ask in ASKS:
            got = self.st.recall("nia", "group", ask, channel=LISTED, now=T0 + 1200)
            self.assertIn("咖啡店", got, f"the earlier private plan is missing from the prompt for: {ask}")
            self.assertNotIn(NO_MEMORY_NOTE, got)
            # a line not yet turned into a memory is shown with its own label, and in a listed group that label
            # gives the same leave the note for stored memories gives (below)
            self.assertIn("房主私聊·私密：这里是小屋自己的群，想提可以自然地提", got)
            self.assertNotIn("别在群里说出来", got)
            self.assertNotIn(PRIVATE_IN_GROUP_NOTE_HUSH, got)

    def test_unlisted_group_also_knows_it_but_is_told_to_keep_quiet(self) -> None:
        for ask in ASKS:
            got = self.st.recall("nia", "group", ask, channel=UNLISTED, now=T0 + 1200)
            self.assertIn("咖啡店", got, f"the earlier private plan is missing from the prompt for: {ask}")
            self.assertIn("别在群里说出来", got)  # the label of a line not yet turned into a memory
            self.assertNotIn(PRIVATE_IN_GROUP_NOTE_FREE, got)

    def test_a_stored_private_memory_comes_with_the_free_note_in_a_listed_group_and_the_hush_note_elsewhere(self) -> None:
        # the same plan once it is a memory (the old test had only `seen` rows)
        self.st.add("nia", "private", "房主打算下个月辞职去开一家咖啡店，这件事先别告诉别人。", channel="private:nia",
                    about=["房主"], kind="event", now=T0 + 700)
        got = self.st.recall("nia", "group", ASKS[1], channel=LISTED, now=T0 + 1200)
        self.assertIn("咖啡店", got)
        self.assertIn(PRIVATE_IN_GROUP_NOTE_FREE, got)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE_HUSH, got)
        got = self.st.recall("nia", "group", ASKS[1], channel=UNLISTED, now=T0 + 1200)
        self.assertIn("咖啡店", got)
        self.assertIn(PRIVATE_IN_GROUP_NOTE_HUSH, got)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE_FREE, got)

    def test_a_question_about_something_never_told_still_gets_the_plain_no(self) -> None:
        # names a topic, does not say it was told in private, nothing matches: no dump of the latest private lines
        for ch in (LISTED, UNLISTED):
            got = self.st.recall("nia", "group", "妮娅，你记得我对什么过敏吗？", channel=ch, now=T0 + 1200)
            self.assertEqual(got, NO_MEMORY_NOTE)

    def test_with_switch_one_off_a_group_never_sees_it(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = MemoryStore(tmp.name, names={"nia": "妮娅"}, env={})
        self.addCleanup(st.close)
        st.observe("nia", "private", LINE1, channel="private:nia", speaker="房主", speaker_role="owner", reply="好的", now=T0)
        for ask in ASKS:
            got = st.recall("nia", "group", ask, channel=LISTED, now=T0 + 1200)
            self.assertNotIn("咖啡店", got)
        # positive control: the same lines reach the same group once the switch is on
        self.assertIn("咖啡店", self.st.recall("nia", "group", ASKS[1], channel=LISTED, now=T0 + 1200))


class DirectBrainPrompt(unittest.TestCase):
    def test_the_rule_against_retelling_private_talk_leaves_room_for_the_listed_group_note(self) -> None:
        src = (ROOT / "src" / "ai_house" / "chat_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("也不要转述房主私聊里说的事。", src)
        self.assertIn("只有这一轮给你的记忆里明确写着在这里可以提时才例外", src)


if __name__ == "__main__":
    unittest.main()
