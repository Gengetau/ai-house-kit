"""Real summaries of "buy X and bring it home" errands, seen in a live run, must compile; anything with a clause
that carries meaning (negation, condition, a second action, another person) must still block completion.
Names: housemate 奈尔, owner 房主, the owner's chat nickname 阿澄."""
import unittest
from pathlib import Path

from worldkernel import promise_rules as P, vocab_life1 as V

PACK = str(Path(__file__).resolve().parents[1] / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
UNSUPPORTED = [{"kind": "unsupported"}]
PARTY = ["nell"]


def errand(item, qty):
    return [{"kind": "purchased", "item": item, "qty": qty}, {"kind": "brought_home", "item": item, "qty": qty}]


TEA = errand("black_tea", 1)


class Base:
    def compile(self, text, party=PARTY, beneficiary=None):
        return self.rules.compile_terms(text, self.vocab, party, beneficiary)


class RealSummaries(Base, unittest.TestCase):
    rules, vocab = P, V

    def test_1_plain_errand_still_works(self):
        self.assertEqual(self.compile("奈尔答应帮房主买一盒红茶带回来"), TEA)

    def test_2_date_note_in_brackets(self):
        self.assertEqual(self.compile("奈尔今天（11月21日）帮房主买一盒红茶带回来"), TEA)

    def test_3_on_the_way_bring_without_buy(self):
        self.assertEqual(self.compile("奈尔11月22日顺路给房主带两盒牛奶回来"), errand("milk", 2))

    def test_4_pronoun_after_help(self):
        self.assertEqual(self.compile("奈尔答应房主今天（11月21日）帮他带一盒红茶回来"), TEA)

    def test_5_owner_nickname_when_the_beneficiary_is_the_owner(self):
        self.assertEqual(self.compile("替阿澄买一盒红茶带回来", beneficiary=["owner"]), TEA)

    def test_5_nickname_is_not_stripped_without_the_owner_as_beneficiary(self):
        for b in (None, [], ["nell"], ["owner", "nell"]):
            self.assertEqual(self.compile("替阿澄买一盒红茶带回来", beneficiary=b), UNSUPPORTED, b)
        self.assertEqual(self.compile("替阿澄买一盒红茶带回来", party=None), UNSUPPORTED)

    def test_6_second_clause_cooking_stays_unsupported(self):
        # `做咖喱` is a second action. It must not be dropped, nor be read as buying curry roux.
        self.assertEqual(self.compile("奈尔11月23日买三个土豆，给房主做咖喱"), UNSUPPORTED)
        self.assertEqual(self.compile("奈尔买三个土豆，给房主做咖喱"), UNSUPPORTED)
        self.assertEqual(self.compile("替阿澄买红茶带回来然后做咖喱", beneficiary=["owner"]), UNSUPPORTED)
        self.assertEqual(self.compile("奈尔11月23日买三个土豆"), [{"kind": "purchased", "item": "potato", "qty": 3}])


class Variants(Base, unittest.TestCase):
    rules, vocab = P, V

    def test_date_forms(self):
        for text in ("奈尔11月21号帮房主买一盒红茶带回来", "奈尔2026年11月21日帮房主买一盒红茶带回来",
                     "奈尔今天(11月21日)帮房主买一盒红茶带回来", "奈尔今天（11月21号）帮房主买一盒红茶带回来",
                     "奈尔（周六）帮房主买一盒红茶带回来", "奈尔11月21日（周六）下午3点帮房主买一盒红茶带回来",
                     "奈尔今天（周六，下午3点）帮房主买一盒红茶带回来", "奈尔当天帮房主买一盒红茶带回来",
                     "奈尔2026-11-21帮房主买一盒红茶带回来"):
            self.assertEqual(self.compile(text), TEA, text)

    def test_filler_words(self):
        self.assertEqual(self.compile("奈尔顺便给房主买一盒红茶带回来"), TEA)
        self.assertEqual(self.compile("奈尔顺路给你买一盒红茶带回来"), TEA)

    def test_bring_home_without_buy(self):
        self.assertEqual(self.compile("奈尔带一盒红茶回来"), TEA)
        self.assertEqual(self.compile("奈尔带红茶回来"), TEA)
        self.assertEqual(self.compile("奈尔带红茶回家"), TEA)
        self.assertEqual(self.compile("奈尔给房主带两盒牛奶回家"), errand("milk", 2))
        self.assertEqual(self.compile("奈尔去超市带一盒红茶回来"),
                         [{"kind": "arrived", "place": "place:supermarket"}] + TEA)

    def test_bring_something_that_is_not_an_ingredient(self):
        for text in ("奈尔把伞带回来", "奈尔带伞回来", "奈尔带伞回家", "奈尔带红茶和伞回来", "奈尔带回来"):
            self.assertEqual(self.compile(text), UNSUPPORTED, text)

    def test_existing_forms_are_unchanged(self):
        self.assertEqual(self.compile("奈尔买2个鸡蛋"), [{"kind": "purchased", "item": "egg", "qty": 2}])
        self.assertEqual(self.compile("奈尔买一盒红茶带回家"), TEA)

    def test_alias_fallback_is_narrow(self):
        owner = ["owner"]
        self.assertEqual(self.compile("给阿澄带一盒红茶回来", beneficiary=owner), TEA)
        # the nickname plus another person is not only the owner
        self.assertEqual(self.compile("替阿澄和小明买红茶带回来", beneficiary=owner), UNSUPPORTED)
        # not a nickname: reflexive, helper words, or no name at all in the shape
        self.assertEqual(self.compile("给自己买红茶", beneficiary=owner), UNSUPPORTED)
        self.assertEqual(self.compile("帮忙买一盒红茶带回来", beneficiary=owner), UNSUPPORTED)
        # a name elsewhere in the sentence is still residue
        self.assertEqual(self.compile("阿澄让奈尔买红茶", beneficiary=owner), UNSUPPORTED)


class StillBlocksAutomaticCompletion(Base, unittest.TestCase):
    """The design principle: never drop a clause that carries meaning. These are pinned as they behave today."""
    rules, vocab = P, V

    def test_negation_condition_and_exclusion(self):
        for text in ("奈尔明天不买红茶了", "如果下雨奈尔就不去买红茶", "奈尔买红茶，不要牛奶", "奈尔买红茶或者牛奶",
                     "奈尔明天买红茶但是不带回来", "奈尔买红茶没买到", "奈尔买红茶（不含牛奶）",
                     "奈尔买红茶（周六，如果下雨就不买）", "奈尔今天（11月21日）不买红茶了",
                     "如果下雨奈尔11月21日就不带红茶回来"):
            self.assertEqual(self.compile(text), UNSUPPORTED, text)

    def test_another_person_as_participant_or_beneficiary(self):
        self.assertEqual(self.compile("奈尔给小明买红茶带回来"), UNSUPPORTED)
        self.assertEqual(self.compile("奈尔给小明买红茶带回来", beneficiary=["nell"]), UNSUPPORTED)
        self.assertEqual(self.compile("奈尔帮小明带一盒红茶回来"), UNSUPPORTED)
        # a pronoun is only taken for the person it is done for, right after 帮/替/给/答应
        self.assertEqual(self.compile("奈尔买红茶，他带回来"), UNSUPPORTED)

    def test_second_action_is_a_second_term_never_dropped(self):
        # today's behaviour: the second action becomes its own term (here: arriving at the park)
        self.assertEqual(self.compile("奈尔买一盒红茶带回来然后去公园"),
                         [{"kind": "arrived", "place": "place:park"}] + TEA)
        self.assertEqual(self.compile("奈尔买一盒红茶带回来然后修电脑"), UNSUPPORTED)

    def test_two_housemates_are_counted_not_dropped(self):
        self.assertEqual(self.compile("奈尔和艾莎买红茶", party=["nell", "aisha"]),
                         [{"kind": "purchased", "item": "black_tea", "qty": 1}])


class OtherPack(Base, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vocab = V.load_pack(PACK)
        cls.rules = P.bound(cls.vocab)

    def test_same_behaviour_in_a_bound_pack(self):
        sora = ["sora"]
        egg = errand("egg", 2)
        self.assertEqual(self.compile("苍空今天（11月21日）顺路给房主带两个鸡蛋回来", sora), egg)
        self.assertEqual(self.compile("苍空11月21号帮他买2个鸡蛋带回来", sora), egg)
        self.assertEqual(self.compile("苍空带2个鸡蛋回家", sora), egg)
        self.assertEqual(self.compile("替阿澄买2个鸡蛋带回来", sora, ["owner"]), egg)
        self.assertEqual(self.compile("替阿澄买2个鸡蛋带回来", sora), UNSUPPORTED)

    def test_bound_pack_negatives(self):
        sora = ["sora"]
        for text in ("苍空把伞带回来", "苍空明天不买鸡蛋了", "如果下雨苍空就不去买鸡蛋", "苍空买鸡蛋，不要牛奶"):
            self.assertEqual(self.compile(text, sora), UNSUPPORTED, text)

    def test_the_default_bound_rules_match_module_functions(self):
        r = P.bound(V)
        for text, b in (("奈尔今天（11月21日）帮房主买一盒红茶带回来", None), ("替阿澄买一盒红茶带回来", ["owner"]),
                        ("奈尔11月22日顺路给房主带两盒牛奶回来", None), ("奈尔把伞带回来", None)):
            self.assertEqual(r.compile_terms(text, V, PARTY, b), P.compile_terms(text, V, PARTY, b), text)


class WhenAdverbials:
    """`出门时` and the like say only when; they are dropped like 下次. Anything else around them still blocks."""
    name, party = "奈尔", PARTY

    def c(self, text, beneficiary=["owner"]):
        return self.compile(text.replace("奈尔", self.name), self.party, beneficiary)

    def test_going_out_is_a_when_word(self):
        self.assertEqual(self.c("奈尔答应房主：11月21日出门时顺路买一盒红茶带回来"), TEA)
        self.assertEqual(self.c("奈尔答应房主：11月23日出门时帮买六个鸡蛋"),
                         [{"kind": "purchased", "item": "egg", "qty": 6}])
        for when in ("出门的时候", "出去的时候", "出去时", "出门", "等下", "等会儿", "等会", "待会儿", "待会", "一会儿"):
            self.assertEqual(self.c("奈尔答应房主：" + when + "顺路买一盒红茶带回来"), TEA, when)

    def test_what_the_when_word_must_not_turn_into_an_errand(self):
        for text in ("奈尔答应房主：下次出门先去找红茶，买到马上告诉房主", "奈尔答应房主：出门前把红茶喝完",
                     "奈尔答应房主：出门时不买红茶", "奈尔答应房主：出门时顺路做咖喱"):
            self.assertEqual(self.c(text), UNSUPPORTED, text)

    def test_day_after_the_day_after_tomorrow_is_a_date_note(self):
        a = self.compile("和房主11月25日（大后天）一起去公园散步", self.party_pair)
        self.assertEqual(a, self.compile("和房主11月25日一起去公园散步", self.party_pair))
        self.assertEqual(self.c("奈尔答应房主：11月24日（后天）帮房主带一瓶可乐回来"), errand("cola", 1))


class WhenAdverbialsDefault(WhenAdverbials, Base, unittest.TestCase):
    rules, vocab = P, V
    party_pair = ["nell", "owner"]


class WhenAdverbialsBound(WhenAdverbials, Base, unittest.TestCase):
    name, party = "苍空", ["sora"]
    party_pair = ["sora", "owner"]

    @classmethod
    def setUpClass(cls):
        cls.vocab = V.load_pack(PACK)
        cls.rules = P.bound(cls.vocab)


class BareBring:
    """Made for the owner, `带<item>` with no `回来` is the same errand as `带<item>回来`; otherwise nothing changes."""
    name, party = "奈尔", PARTY

    def c(self, text, beneficiary=["owner"]):
        return self.compile(text.replace("奈尔", self.name), self.party, beneficiary)

    def test_bare_bring_for_the_owner_is_an_errand(self):
        """Fails if `带一包薯片` / `带两盒牛奶` for the owner stays unsupported, or loses an event or the count."""
        self.assertEqual(self.c("奈尔答应房主下次出门顺路带一包薯片"), errand("chips", 1))
        self.assertEqual(self.c("奈尔答应房主：明天带两盒牛奶"), errand("milk", 2))
        self.assertEqual(self.c("奈尔答应房主：带一包薯片给他。"), errand("chips", 1))

    def test_without_the_owner_as_beneficiary_it_stays_unsupported(self):
        """Fails if the bare form is read as an errand when nobody says it is for the owner."""
        for b in ([], None, ["nell"], ["owner", "nell"]):
            self.assertEqual(self.c("奈尔答应房主：明天带两盒牛奶", b), UNSUPPORTED, b)
            self.assertEqual(self.c("奈尔答应房主下次出门顺路带一包薯片", b), UNSUPPORTED, b)

    def test_not_an_ingredient_or_not_an_errand_stays_unsupported(self):
        """Fails if anything but an ingredient, or a trip somewhere, turns into a purchase."""
        for text in ("奈尔答应房主：带伞", "奈尔答应房主：带他去公园", "奈尔答应房主：带一包薯片去公园",
                     "奈尔答应房主：带一包薯片和伞", "奈尔答应房主：不带薯片"):
            for b in (["owner"], None):
                got = self.c(text, b)
                self.assertEqual(got, UNSUPPORTED, text)
                self.assertFalse([x for x in got if x["kind"] == "purchased"], text)


class BareBringDefault(BareBring, Base, unittest.TestCase):
    rules, vocab = P, V


class BareBringBound(BareBring, Base, unittest.TestCase):
    name, party = "苍空", ["sora"]

    @classmethod
    def setUpClass(cls):
        cls.vocab = V.load_pack(PACK)
        cls.rules = P.bound(cls.vocab)


if __name__ == "__main__":
    unittest.main()
