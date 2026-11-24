"""wanda PR #25 review R25-01: recalling across chats never widens what a chat may change or forget; and the
说说 material (today public=True) has no private memories or diaries."""
import unittest

import memory
from test_memory_unified import G, G2, OWNER_DM, UnifiedTests


class Rights(UnifiedTests):
    def test_group_cannot_change_or_forget_owner_private_memory(self):
        mid = memory.add("mina", OWNER_DM, "房主喜欢喝乌龙茶", kind="preference")[0]
        with self.assertRaises(ValueError):
            memory.update("mina", G2, mid, "房主其实最讨厌乌龙茶")
        with self.assertRaises(ValueError):
            memory.forget("mina", G2, mid)
        memory.update("mina", OWNER_DM, mid, "房主喜欢喝冰乌龙茶")  # the owner's DM still can

    def test_group_cannot_change_another_groups_memory(self):
        mid = memory.add("mina", G, "路人甲喜欢猫", kind="preference")[0]
        with self.assertRaises(ValueError):
            memory.update("mina", G2, mid, "路人甲讨厌猫")
        memory.update("mina", G, mid, "路人甲很喜欢猫")

    def test_extractor_cannot_rewrite_private_from_a_group(self):
        mid = memory.add("mina", OWNER_DM, "房主喜欢喝乌龙茶", kind="preference")[0]
        memory._apply("mina", G2, [{"op": "update", "id": mid, "text": "房主讨厌乌龙茶"},
                                     {"op": "forget", "id": mid}], False, False, "auto")
        with memory.db() as c:
            row = c.execute("select text, status from mem where id=?", (mid,)).fetchone()
        self.assertEqual((row["text"], row["status"]), ("房主喜欢喝乌龙茶", "active"))

    def test_public_material_has_no_private_or_diary(self):
        memory.diary("mina", G2, "今天其实有点吃醋，房主一直夸别人，我嘴上说没事。")
        memory.add("mina", G2, "群里今天在聊猫咖", kind="event")
        text = memory.today("mina", G2, public=True)["text"]
        self.assertIn("猫咖", text)
        self.assertNotIn("拉面", text)  # owner-DM memory
        self.assertNotIn("吃醋", text)
        self.assertNotIn("前几天的日记", text)

    def test_extractor_in_a_group_is_not_shown_private_memories(self):
        self.assertIn("拉面", memory.recall("mina", G2, ["[路人|42] (message_id=1): 拉面"]))
        self.assertNotIn("拉面", memory._related("mina", G2, "拉面好吃吗"))
        self.assertIn("拉面", memory._related("mina", OWNER_DM, "拉面好吃吗"))


del UnifiedTests  # run its tests once, in its own module


if __name__ == "__main__":
    unittest.main()
