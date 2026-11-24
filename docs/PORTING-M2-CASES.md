# M2 切换：kit 原有约定用例逐个处理的清单

配 `docs/PORTING-PLAN.md` 15.17 看。小屋的约定换成与私有部署共用的那一份代码（`src/ai_house/common/intents.py`）、
kit 原来自己的实现（`worldkernel/intents_lite.py`）删除之后，原来对着旧实现写的用例分给五组分别处理，每个用例三选一：

- **K 保留**：原样通过，或只改了导入行、函数签名。
- **R 改写**：用例保护的那条保证还在，改成经由新路径（整理记忆时由模型提出、程序核对原话）去查同一条保证。
- **D 退役**：所测的只是按“与私有一致”去掉的 kit 独有做法。表里写明是哪一条（D1–D8），以及等价的保证现在测在哪里。

去掉的做法编号：

- D1 聊天当时按规则登记（含识别“出门/一起”、时间词定期限、同一计划合并、只认房主提出、睡着时在登记这一步的特殊处理）
- D2 住客的 `record_promise` 工具及其在各层的传递
- D3 饮食禁忌备忘作为约定（现在是记忆；旧库里开着的备忘启动时带进记忆一次）
- D4 买不到的东西记成“提醒”（回复后面那句“买不了”保留）
- D5 过期时给房主发通知
- D6 按种类结单、固定 24 小时有效期、回复前的“约定提示”（换成 `INTENTS_SHOW` 下的 `card_lines`，默认关）
- D7 `POST /api/intents/cancel`（现在回 501）
- D8 旧表 `house_intents`（升级的小屋里留在库里不读；新小屋没有）

规矩：不跳过、不标“预期失败”、不为了通过而放松断言；“不应该发生”的断言必须在同一场景里有一个“该发生的确实发生了”的对照；
看着像真缺陷的不许绕过去，留着失败并上报。五组交回时都没有留下失败的用例；交回的提问及其处置写在 15.17 的“变差的地方”和“没有验证的”里。

下面各组的表是处理人交回的原文（英文）。

## Group g1: `test_realrun_1_promises.py`, `test_realrun_4_round2.py`, `test_private_memory_groups.py`

Run: `Ran 103 tests ... OK` for the three modules together (12 + 11 + 80).

### `test_realrun_1_promises.py`

Before: 24 cases (the module did not load; it imported `worldkernel.intents_lite`). After: 12 cases, all pass.
Counts of the 24: K 1, R 11, D 12 (several R cases were merged into one, see the table).

The module still exports `H`, `JST`, `T0`, `PACK`, `_CapturingModel`, `_DaemonCase` (used by `test_private_memory_groups.py`
and others). `_StoreCase` keeps its name but is now a fixture over `CommonIntents` (the shared promise code, standalone,
with the kernel-evidence switches the daemon sets): `chat(consumer, [(who, text)], ops)` feeds one memory batch to
`apply_chat`, `evidence(body)` writes a row of the kernel's evidence stream, `sweep(now)`, `rows(actor)`. The old
`say(...)` (rule-based recording from the chat turn) does not exist any more. New helper `plan_ops`.

| case | K/R/D | what it protects now, or which dropped item and where the equivalent is tested |
|---|---|---|
| SmallTalkIsNotAPromise.test_chatter_records_nothing | D (D1) | keyword scan of the owner's line and the reply. Whether a sentence is a plan is now the extraction model's call; the host's gates (quote in the batch, said by someone in it, not a joke / dream / question) are covered by `intents-conformance-tests/test_intents.py` FoundTests (`test_talking_is_not_saying_yes`, `test_quote_must_be_in_the_batch`) and `test_intents_evidence.py` EvidenceTests (`test_done_fiction_negation_joke_and_question`). That the chat turn alone records nothing: `test_m2_intents_adapter.py` EndToEndTests `test_the_chat_turn_alone_records_nothing` |
| SmallTalkIsNotAPromise.test_keyword_only_in_the_reply_records_nothing | D (D1) | same: a keyword scan of the reply |
| SmallTalkIsNotAPromise.test_real_invitations_and_requests_are_still_recorded | D (D1) | the positive control of the scan. Recording through extraction is covered by `test_m2_intents_adapter.py` `EndToEndTests.run_errand` and OwnerNameTests; joint promises by conformance `FoundTests.test_joint_needs_both_to_speak` |
| UnrelatedHomeEventsDoNotCloseOutings.test_tea_at_home_does_not_close_an_outing | R | the promise is a "go to the park / bookshop / library" plan; a tea party that finished at home does not close it, the arrival at the named place (control, same scenario) does. Generic rules of `promise_rules` |
| UnrelatedHomeEventsDoNotCloseOutings.test_shopping_trip_does_not_close_a_park_outing | R | arrival at and plan finished at the supermarket leave a park promise open; the later arrival at the park closes it |
| UnrelatedHomeEventsDoNotCloseOutings.test_arriving_at_the_named_place_still_closes | R | now `test_someone_elses_arrival_at_the_named_place_does_not_close_it`: another resident's arrival at the park does not close wanda's promise, wanda's own does and `evidence_key` names that evidence. Closing by arrival itself is also in the two cases above |
| UnrelatedHomeEventsDoNotCloseOutings.test_finished_plan_away_from_home_still_closes_an_unnamed_outing | D (D6) | kind-based closing ("outing" closed by any finished plan away from home). An outing with no named place is now not closed by the kernel at all (generic rules: `plan_finished` never satisfies a term; it ends by people saying so or by time). Nothing tests this directly; see report |
| UnrelatedHomeEventsDoNotCloseOutings.test_together_tea_is_not_closed_by_a_different_activity_but_by_tea | R | now `test_together_film_is_not_closed_by_a_different_activity_but_by_the_film` (the kit's tea wording is not a closed-grammar activity: "茶会" loses its 会 in the rules). A joint promise of two to watch a film is not closed by both playing a game at home, and is closed by both having the film night (control) |
| DecliningReplyIsNotAnAgreement.test_resident_who_refused_gets_no_errand | R | merged into `test_resident_who_refused_gets_no_errand_and_the_one_who_agreed_keeps_hers`: the model lists 阿尔玛 as agreed but her line refuses, so the host records nothing and no errand; 奈尔, who said yes to the same sentence, gets a promise and an `errands()` entry for 2 eggs (control) |
| DecliningReplyIsNotAnAgreement.test_resident_who_agreed_to_the_same_sentence_keeps_hers | R | the control above |
| DecliningReplyIsNotAnAgreement.test_refusal_also_blocks_the_llm_tool_intent_and_other_plans | R | now `test_a_refusal_never_makes_an_accepted_plan_or_promise`: the tool-intent part is gone (D2); a counter-question to a walk and a "no way" to a cooking request never give an accepted row (the walk stays "proposed" with only the owner agreed; the cooking promise is rejected); 婉妲's plain yes (control) is accepted |
| DecliningReplyIsNotAnAgreement.test_agreement_with_a_caveat_is_still_an_agreement | D (D1) | the kit read the first deciding clause of the reply ("好的，我会买的，不过现在还没法出门"). The shared module accepts a yes only when the rest of the reply is empty, on the topic or a time/going continuation, so this reply is not agreed. Not tested any more; see the question in the report |
| DecliningReplyIsNotAnAgreement.test_diet_memo_is_not_a_promise_and_survives_a_refusing_tone | D (D3) | diet memos are memories now. The carry-over of old open memo rows: `test_m2_intents_adapter.py` CarryOverTests |
| NamesAndNounsAreNotIngredients.test_mina_is_not_rice | R | the record-by-rule part is gone (D1); now `buy_words.detect_errand_item` on the real sentence is None, and a request for rice with her name in it (control) is rice |
| NamesAndNounsAreNotIngredients.test_single_char_alias_needs_to_be_the_thing_bought | R | same assertions through `buy_words.detect_errand_item(vocab, text)` (was a private method of the store); a fish request added as control |
| NamesAndNounsAreNotIngredients.test_buying_rice_or_fish_still_works | R | same assertions through `buy_words` |
| NamesAndNounsAreNotIngredients.test_resident_names_are_not_matched_even_for_longer_aliases | R | same through `buy_words`; control: the same request without the name |
| ResidentKnowsItCannotBuyBeforeAnswering.test_the_prompt_says_the_item_cannot_be_bought | R | `reply_hints` (D6) put the hint in the resident's prompt; now the guarantee is the reply tail: `ReplyTailSaysItCannotBeBought.test_a_request_for_something_not_sold_gets_the_tail_and_a_purchasable_one_does_not` checks the exact tail 「不过「榴莲」我这边买不了，没法替你带回来。」 after her own words |
| ResidentKnowsItCannotBuyBeforeAnswering.test_a_purchasable_item_gets_no_such_hint | R | merged into the case above: the milk request gets no "买不了" |
| ResidentKnowsItCannotBuyBeforeAnswering.test_already_declining_reply_is_not_given_a_second_contradicting_tail | K | unchanged idea (`reply_declines`), now also asserts that the tail is absent |
| ResidentKnowsItCannotBuyBeforeAnswering.test_followup_after_expiry_tells_her_it_expired_and_was_never_buyable | D (D6, D4) | prompt hint after expiry for a reminder row (`raw_item`); neither reply hints nor reminder rows exist. Closest: `test_m2_intents_adapter.py` ShownTests (`card_lines` only under `INTENTS_SHOW`) |
| ResidentKnowsItCannotBuyBeforeAnswering.test_followup_about_an_expired_catalog_errand_says_it_was_not_bought | D (D6, D1) | prompt hint about an expired errand, and the errand was recorded by rule |
| ResidentKnowsItCannotBuyBeforeAnswering.test_unrelated_chat_gets_no_promise_hint | R | merged into the tail case: unrelated chat afterwards gets no "买不了" |
| ResidentKnowsItCannotBuyBeforeAnswering.test_expiry_notice_for_a_reminder_does_not_claim_a_promise_to_buy | D (D5, D4) | the expiry notice to the owner and reminder rows are gone. Quiet expiry: conformance `test_intents.py` EndTests `test_quiet_expiry_and_restore` |

### `test_realrun_4_round2.py`

Before: 17 cases (the module did not load). After: 11 cases, all pass. The seven fact-list cases (`SharedMealIsPerDish`,
`WalksThatGoNowhereAreNotLines`) are K, only the import line changed.

| case | K/R/D | what it protects now, or which dropped item and where the equivalent is tested |
|---|---|---|
| GoingOutTogetherIsRecorded.test_the_real_sentence_is_recorded_as_an_outing_for_tomorrow_morning | D (D1) | recording by rule with kind `outing` and a window parsed from the sentence. The window now comes from the model's `due` (conformance `FoundTests.test_joint_needs_both_to_speak` checks `due_from` / `due_to` of a given date and time) |
| GoingOutTogetherIsRecorded.test_invitations_in_other_words | D (D1) | the keyword list of plans |
| GoingOutTogetherIsRecorded.test_looking_back_or_small_talk_is_not_a_plan | D (D1) | `owner_proposes_plan` and the rule recording; see the small-talk cases above |
| GoingOutTogetherIsRecorded.test_buying_clothes_is_not_a_supermarket_errand | R | now `CompanionShoppingIsNotAnErrand`: "陪我去…买衣服" gives no item, no request noun and no unbuyable noun in `buy_words`; a real egg request and a durian request (controls) do |
| GoingOutTogetherIsRecorded.test_an_agreed_outing_closes_on_arrival_and_expires_otherwise | R | now `AnAgreedOutingClosesOnArrivalAndExpiresOtherwise`: a promise with a window closes on the arrival at the named place and names the evidence; with no evidence it is still open at the evening of the window and expired the next morning (quiet). New case in the same class: an arrival before the promise was made does not close it, the arrival in the window does |
| GoingOutTogetherIsRecorded.test_a_reply_that_declines_still_records_nothing | R (moved) | now in `test_realrun_1_promises.py` `DecliningReplyIsNotAnAgreement.test_a_refusal_never_makes_an_accepted_plan_or_promise` (walk invitation answered by a counter-question) |
| TimeWordsSetTheWindow.test_this_afternoon_tea_is_not_closed_by_tomorrows_tea_party | R | now `ATimeGivenBoundsThePromise.test_this_afternoon_film_is_not_closed_by_tomorrows_film_night` (film night: the tea wording is not in the closed grammar): the film night of the next day does not close a promise with an afternoon window, the promise ends as missed; the same-afternoon film night (control, second house) closes it and is named as evidence |
| TimeWordsSetTheWindow.test_the_same_afternoon_tea_party_still_closes_it | R | the control above |
| TimeWordsSetTheWindow.test_without_a_time_word_the_window_is_the_old_24_hours | D (D6) | the fixed 24 h lifetime. The shared module's expiry table (no time given: 14 days, 30 for vague / after; dated ones 12 h after the window) is in conformance `test_intents_kernel191.py` `Rules.test_expiry_table` |
| TimeWordsSetTheWindow.test_parse_due_window_periods | D (D1) | `parse_due_window` is gone; the model gives `due` as a date and time and `_due` reads it (conformance `FoundTests.test_joint_needs_both_to_speak`) |

### `test_private_memory_groups.py`

Before: 80 cases, the module did not load (it imports `PACK`, `T0`, `_CapturingModel` from `test_realrun_1_promises`).
After: 80 cases, all pass, file not changed (`git diff` empty for it); all three names are still exported.

## Group 2: `test_realrun_7_round3.py`, `test_realrun_8_round4.py`, `test_realrun_9_leftovers.py`

Result: 63 cases in three files, all pass. Before: none of the three modules loaded (`worldkernel.intents_lite` and
`test_realrun_1_promises._StoreCase` are gone / old), so every case was an error.

| file | cases before | cases after | K | R | D |
|---|---|---|---|---|---|
| test_realrun_7_round3.py | 25 | 25 | 21 (one with a dropped assertion replaced) | 4 | 0 |
| test_realrun_8_round4.py | 34 | 16 | 10 (diary) | 6 | 18 |
| test_realrun_9_leftovers.py | 22 | 22 | 16 | 5 | 1 retired, replaced by 1 new case |

Common helper change: the three files no longer import `_StoreCase` / `_DaemonCase` / `_CapturingModel` / constants from
`test_realrun_1_promises` (which is being rewritten by another group). `test_realrun_7_round3.py` now holds them:
`T0`, `H`, `PACK`, `NAMES`, `_CapturingModel`, `_DaemonCase`, and `_PromiseCase`, a fixture over a fresh `CommonIntents`
(built with `test_m2_intents_adapter.standalone`, the daemon's switches, a `kernel_bind` row) whose `say(owner_text,
{actor: reply})` applies "an extraction answer that always claims she agreed" to the batch lines through `apply_chat`.
What is recorded is therefore decided by the shared module's own checks of what the lines say. Files 8 and 9 import
these from file 7. `rows()` reads the `intents` table directly (`listing` hides private rows' text).

### test_realrun_7_round3.py

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| import line | K | `reply_declines` now comes from `worldkernel.buy_words`; the four `RefusalPhrases` cases are unchanged and pass |
| SlackerRepliesMakeNoErrand.test_gemini_alma_overslept_and_is_not_going_out | R | a reply that says "not going out" is not recorded as agreeing even though the extraction answer claims it (shared `_bound_yes`: no yes, so "a promise she did not say yes to"); control: the same request answered "好呀" becomes an accepted promise and an errand for 2 eggs |
| SlackerRepliesMakeNoErrand.test_terra_alma_gives_only_a_conditional_maybe | R | same for the two real conditional-maybe wordings; same control |
| SlackerRepliesMakeNoErrand.test_somebody_else_who_agrees_keeps_the_errand | R | alma's refusal is not recorded, nell's yes to the same request is; the errand is 2 eggs for nell only and none for alma (no leak between housemates) |
| SlackerRepliesMakeNoErrand.test_real_agreements_still_become_promises | R | three real milk agreements become accepted private promises and a 1-milk errand; the real refusal of the same request in the same test does not. Removed: the "wanda tea becomes kind `together`" half (D1, kind-based recording; the shared module has no such kind. Invitations to joint plans: `intents-conformance-tests/test_intents_invites191.py`) |
| UnbuyableItemGetsOneSentenceNotTwo.test_a_reply_that_says_it_cannot_buy_it_gets_no_second_sentence | K | one assertion changed: `assertNotIn("只先记个提醒")` (D4, that tail is gone) became `assertNotIn("我这边买不了")`, the kept tail. The `assertEqual(text, reply)` is unchanged; the control is the next case (a vague reply does get the tail) |

### test_realrun_8_round4.py

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| GroceryRunDoesNotCloseAnOutingPromise.test_the_real_case_stays_open_then_ends_as_missed | R | the real grocery-run evidence (arrival at the supermarket, plan finished) does not close 「陪房主去逛街买衣服」, which is still open after the sweep and ends as `missed` once its day has passed; control: a second promise of hers 「去超市」 is completed by the same evidence. Removed: the told-as-not-gone hint (D6) |
| GroceryRunDoesNotCloseAnOutingPromise.test_a_named_place_closes_it_and_other_places_do_not | R | promise 「去商店街」 is closed by arriving at the shopping street only, not by the park, library or konbini (the shared rules read the place from the text; before it was by kind) |
| GroceryRunDoesNotCloseAnOutingPromise.test_an_outing_with_no_place_named_is_not_closed_by_the_grocery_run | R | the grocery run (arrival, shopping street arrival, finished shopping plan) does not close 「陪房主出去走走」; control in the same scenario: the supermarket arrival closes 「去超市」. Removed: "an arrival at the park / riverside closes a place-less outing" (D6, kind-based closing; a text with no readable place is never closed by evidence now) |
| GroceryRunDoesNotCloseAnOutingPromise.test_a_promise_to_go_to_the_supermarket_is_still_closed_by_going_there | R | same guarantee on the new path, with a check that it is still open before the evidence |
| PromiseHintsForOutingsAndTogether (12 cases) | D | D6: `reply_hints` is gone (replaced by `card_lines`, group promises only, due soon, default off). The guarantee that survives is that card lines reach the chat context only when shown: `test_m2_intents_adapter.py` ShownTests.test_card_lines_reach_the_chat_context_only_when_shown, and `card_lines` itself in the conformance tests. Nothing now tells a resident, when asked, that an expired outing was not gone to (see the questions in the hand-back message) |
| VagueFollowUpBringsBackTheLatestTwo (3 cases) | D | D6, same |
| DaemonGivesTheModelTheFact.test_follow_up_the_next_evening_reaches_the_prompt | D | D6 (the prompt block 「关于你和房主的约定（据实回答）」 is gone); also depended on D1 (the outing recorded by rule from the chat turn) |
| ErrandHintsAreUnchanged.test_expired_catalog_errand_text_is_as_before | R | now `UnbuyableNounAndExpiredErrand.test_an_errand_whose_day_has_passed_ends_quietly_and_stops_being_an_errand`: an errand is offered to the kernel on its day and not before, ends as `missed` after its day without any purchase, is no longer offered, and shows as `expired` on the page. Removed: the hint text (D6) and the fixed 24 h lifetime (D6) |
| ErrandHintsAreUnchanged.test_unbuyable_noun_text_is_as_before | R | now `UnbuyableNounAndExpiredErrand.test_the_noun_behind_the_cannot_buy_tail_is_found_only_for_what_the_catalogue_lacks`: `buy_words.unbuyable_noun` names 「榴莲」 and says nothing for eggs, milk, a person's name or small talk. Removed: the hint text (D6) |
| ErrandHintsAreUnchanged.test_errand_and_outing_hints_together | D | D6 |
| ErrandHintsAreUnchanged.test_an_errand_is_not_repeated_as_a_promise | D | D6 |
| DiaryTextHasNoTitle (10 cases) | K | unchanged; only the imports of the file changed |

### test_realrun_9_leftovers.py

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| SleepingResidentPromisesNothing.test_record_intents_off_keeps_the_owner_line_for_when_she_wakes_but_no_promise | R | now `test_the_owner_line_reaches_the_queue_whether_or_not_she_answered`: the owner's invitation reaches ChatLink's queue with `she_said` empty when she answered nothing, and with her line when she did. Removed: `record_intents=False` (D1) |
| SleepingResidentPromisesNothing.test_default_is_unchanged | D | D1 (rule-based recording of an outing from the chat turn). Replaced by a new case, `test_a_placeholder_in_her_place_is_not_a_yes_but_a_real_yes_is`: even if the sleeping placeholder reached a batch as her line it is no agreement (the shared module's yes check), with the awake control |
| SleepingResidentThroughTheDaemon.test_a_sleeping_placeholder_records_no_promise | R | through the daemon: the placeholder is not in the extraction prompt, no promise is recorded for a resident who answered nothing although the model claims she agreed, and the owner's line reaches `owner_chat_queue` at extraction time with `she_said` empty. Control in the same house: after a wake-up request the same kind of answer is accepted. The queue is read tolerating a missing table; before extraction the case asserts nothing is queued by the chat turn |
| SleepingResidentThroughTheDaemon.test_asking_her_to_wake_up_still_gets_a_real_answer_and_a_promise | R | the awake path: real answer, extraction accepted, promise open, her answer in `she_said` |
| MemoHasNoDueInThePast.test_the_real_sentence_makes_one_memo_that_outlives_its_creation | R | now `test_a_due_in_the_past_does_not_end_the_promise_at_once`: a due written in the past (2026-03-14) is dropped by the shared `_due`, the promise stays open a day later and its deadline is after it was made. Removed: the memo kind and the 3650-day lifetime (D3: a diet or birthday note is a memory, not a row) |
| MemoHasNoDueInThePast.test_a_calendar_date_is_not_a_due_phrase | R | now on the new path: 「生日是3月14日」 in the text gives no due window; control: a due written by the model gives the window 14:00-17:00. Removed: `parse_due_window` (D1) |
| the other 16 cases (diary together, day note, brought home) | K | unchanged except the import line |

## Group 3: tail_2, tail_10, tail_4a, tail_7b, agent_1, agent_2

All six files load and pass in full (159 cases, `Ran 159 tests ... OK`, about 90 s).

| file | cases before | cases after | before the work |
|---|---|---|---|
| test_tail_2_memory_intents.py | 30 | 27 | import failure (`worldkernel.intents_lite`) |
| test_tail_10_promise_tail.py | 13 | 8 | 12 failing or erroring |
| test_tail_4a_llm_deadline.py | 35 | 35 | 1 error (signature) |
| test_tail_7b_deadline_step.py | 15 | 15 | 1 failure |
| test_agent_1_runtime.py | 55 | 55 | 1 error |
| test_agent_2_house.py | 19 | 19 | 1 failure |

Helper changes: tail_2 now imports `buy_words`, `promise_rules` and `vocab_life1` instead of `intents_lite`; the old
`_IntentBase` (a `CommitmentStore` on a temp db) is gone. tail_10's loopback model no longer offers or calls tools.
agent_1's `TOOLS` list and `Rig.runner` have a harmless `check_fridge` house tool where `record_promise` was.

### test_tail_2_memory_intents.py (30 -> 27; 14 memory cases untouched)

| case | K/R/D | what it protects now, or which dropped item and where the equivalent is tested |
|---|---|---|
| BuyRequestParsing.test_requests_and_commitments_become_open_errands_with_raw_item | R | renamed `test_requests_and_commitments_name_the_thing_to_buy`: all 22 sentences still yield their noun through `buy_words.extract_buy_request_noun(text, reply)` (including the resident's own "好，我去买" commitments), and the noun is not a catalogue item. The "open errand with `raw_item`" half is dropped (D1, D4) |
| BuyRequestParsing.test_catalog_request_still_resolves_to_item | R | now `buy_words`: "帮我买点牛奶" resolves to `milk` and is not reported unbuyable; control: the same shape with 榴莲 is reported unbuyable |
| BuyRequestParsing.test_chitchat_never_becomes_an_errand | R | `test_chitchat_never_reads_as_a_purchase_request`: the 34 negative sentences yield no noun and are not unbuyable; positive control (all 22 requests yield their noun) in the same run. The second path, "with LLM intents present" (`enqueue_chat(intents=...)`), is dropped (D1) |
| BuyRequestParsing.test_particles_are_stripped_from_raw_item | R | `test_particles_are_stripped_from_the_noun`: same check on the parser's noun instead of the stored `raw_item` (D4 for the column) |
| EvidenceWindowAndStrength (class) | R | replaced by `PromiseEvidenceRules`, which feeds kernel-shaped evidence rows to `promise_rules.bound(V).judge` with terms from `compile_terms`; 12 cases became 9 |
| ...test_invite_settled_does_not_close_outing_or_together + test_plan_finished_only_closes_on_successful_outcome + test_together_plan_finished_needs_more_than_one_member_when_known + test_cook_plan_finished_cancelled_does_not_close | R/D | merged into `test_invite_settled_and_plan_finished_never_satisfy_a_term`: no `invite_settled` / `plan_finished` row (any outcome) closes an arrival, a purchase or a shared activity, with the real evidence as control. The generic judge never lets `plan_finished` satisfy a term. Closing a `together` / `cook` / `outing` by a successful `plan_finished` was kind-based closing (D6) and is dropped. Shared-module counterpart: `intents-conformance-tests/test_intents_kernel191.py` (expiry ignores plan finish from an old stream) covers the plan_finished side of the window |
| ...test_together_closes_on_real_shared_activity_only | R | `test_together_closes_on_real_shared_activity_only`: two housemates' activity closes only with the same plan, the same place, an overlap of at least ten minutes and the named activity; five negative variants plus one genuine control. Note the generic rule counts outcome `cancelled` as well as `succeeded` (not asserted either way) |
| ...test_outing_arrival_at_named_place_still_closes | R | `test_outing_arrival_at_the_named_place_closes`: arrival at the named place closes, arrival elsewhere or by another housemate does not |
| ...test_errand_evidence_after_expiry_does_not_complete | R | `test_errand_evidence_after_the_deadline_does_not_complete`, control inside the window. The old fixed 24 h lifetime is D6; the test sets its own deadline |
| ...test_errand_evidence_inside_window_completes_even_if_swept_late | R | `test_errand_evidence_inside_the_window_completes_even_if_judged_late` |
| ...test_evidence_before_creation_is_ignored | R | same name; control: the same purchase after creation closes it |
| ...test_outing_arrival_after_expiry_does_not_complete | R | `test_outing_arrival_after_the_deadline_does_not_complete`, with control |
| ...test_together_plan_finished_after_expiry_does_not_complete | R | `test_together_activity_after_the_deadline_does_not_complete`: a shared activity that ended after the deadline does not close, control inside |
| ...test_outing_destination_map_matches_real_place_ids | R | `test_place_words_map_to_real_place_ids`: 12 place words compile to the arrival term for the real catalogue id (checked against `places.json`), and only an arrival there closes. The old "去喫茶店" row is not in the shared compiler's vocabulary (it compiles to `unsupported`) and was left out |

### test_tail_10_promise_tail.py (13 -> 8)

| case | K/R/D | what it protects now, or which dropped item and where the equivalent is tested |
|---|---|---|
| UnbuyableItemTests.test_durian_reply_tells_owner_it_cannot_be_bought | R | the reply ends with exactly "不过「榴莲」我这边买不了，没法替你带回来。" after her own words, no "提醒" wording (D4); the reminder row assertion is removed and replaced by "the turn records nothing". The owner text is now "帮我买点榴莲回来" (an explicit request): the old text "买点榴莲回来吧" only worked through the tool's `item` (D2) |
| UnbuyableItemTests.test_durian_without_tool_call_also_warns | R | renamed `test_durian_with_a_reply_that_names_the_item_also_warns` (the tool is gone, so every turn is "without tool call") |
| UnbuyableItemTests.test_catalog_purchase_is_not_warned | R | no tail for a catalogue item, with the 榴莲 request in the same test as control; the milk row assertion is removed (D1, D2) |
| UnbuyableItemTests.test_catalog_purchase_with_empty_item_still_resolves | D | tested only the tool's empty-`item` fallback (D2) and the recorded row (D1). The guarantee that "买两盒牛奶" is a catalogue purchase is asserted in `test_catalog_purchase_is_not_warned` (`detect_errand_item` gives `milk`, 2). Item resolution inside a promise: `test_m2_promise_rules.py::OtherPack::test_a_promise_is_completed`, `test_m2_intents_adapter.py::ListIntentsTests` |
| UnbuyableItemTests.test_expired_promise_is_reported_to_owner_once | D | the expiry notice is dropped (D5). Replaced by `test_an_expired_promise_is_quiet_and_listed_as_expired`: a promise made through extraction is open, becomes `expired` by time (30-day vague window, sweep), and no chat message appears. Conformance: `test_intents.py::test_quiet_expiry_and_restore` |
| UnbuyableItemTests.test_completed_promise_is_not_reported | D | it asserted no expiry note after a completed promise; it was vacuous once no promise could be recorded by the turn, and the notice no longer exists (D5). The quiet-expiry case above covers "no notice" with a positive control |
| (new) UnbuyableItemTests.test_a_reply_that_already_declines_gets_no_second_no | new | `reply_declines`: a reply that already says "买不到" gets no tail; control: an accepting reply to the same request does |
| (new) UnbuyableItemTests.test_catalog_request_with_a_wordy_reply_is_not_warned | new | a long reply naming other things does not turn a catalogue request into an unbuyable one |
| GarbageErrandTests.test_small_talk_with_empty_item_records_nothing | R | `test_small_talk_is_not_read_as_a_purchase_request`: "买卖" / "买单" small talk yields no noun, no tail, no row; control: a real request yields the noun and the tail. The tool-result assertion (`recorded: False`) is dropped (D2) |
| GarbageErrandTests.test_unknown_item_in_owner_sentence_still_recorded_with_raw_item | D | recorded a reminder row with `raw_item` (D1, D4). The noun side is covered by tail_2 `BuyRequestParsing` and the tail by the durian case above |
| (new) GarbageErrandTests.test_non_owner_asking_for_something_gets_no_tail | new | only the owner's request gets the tail (a group member's does not); control: the owner's same request does. Was an untested branch of the daemon code, kept next to the other tail cases |
| QuestionNotMemoTests.test_questions_create_no_memo_without_tool / ..._with_tool | D | memo rows from questions (D1, D2, D3). Guarantee that a chat turn alone records nothing: `test_m2_intents_adapter.py::EndToEndTests::test_the_chat_turn_alone_records_nothing`; standing diet notes are memories now (memory tests of the shared module) |
| QuestionNotMemoTests.test_statements_still_create_memo / test_statement_memo_via_tool_still_recorded | D | memo rows for dietary statements (D1, D2, D3) |
| QuestionNotMemoTests.test_queue_payload_has_no_memo_for_question | D | the payload with `kind: memo` intents (D1, D3); the new queue is written only at extraction (`EndToEndTests` in the adapter tests) |

### test_tail_4a_llm_deadline.py (35 -> 35)

| case | K/R/D | what it protects now |
|---|---|---|
| DeadlineTests.test_tool_loop_slow_rounds_share_one_budget | K | signature fix only (`recorded_intents` argument removed from `_run_tool_loop`) |
| DeadlineTests.test_tool_loop_hanging_endpoint_stops_within_budget | K | same signature fix. It had been passing for the wrong reason: the stale 6-argument call raised `TypeError`, which `assertRaises(Exception)` accepted. It now asserts `LLMAPIError`, so a wrong-signature call can no longer pass |

### test_tail_7b_deadline_step.py (15 -> 15)

| case | K/R/D | what it protects now |
|---|---|---|
| StepAfterReplyTests.test_private_turn_step_still_asks_life_model_after_reply_spent_the_deadline | R | same purpose (after a reply that spent the deadline, the world step still asks the life model, and later steps keep asking). Why it failed: the old chat turn queued an owner-chat decision for the resident, so a fourth life-model request came within three virtual steps; now the turn queues nothing (the owner's line reaches the world at extraction time) and the next free-time decision of that resident is due about two virtual hours later. The follow-up loop now steps up to 18 times of 600 s (3 h) and stops at the first new request; the assertion that a new request is made is unchanged. The first half (fresh budget, no `unknown` ledger rows, the three first requests made) is untouched |

### test_agent_1_runtime.py (55 -> 55)

| case | K/R/D | what it protects now |
|---|---|---|
| SpeakingTests.test_house_tools_run_on_the_callers_thread_and_promises_are_collected | R | renamed `..._and_their_results_are_collected`: the second house tool is `check_fridge` instead of `record_promise`; still asserts that every house tool runs on the caller's thread, the calls are made once and in order with their arguments, the tool result reaches the model, `tool_calls == 2`, and the reply is kept. Dropped: `res.recorded_intents` (D2); now asserts the attribute does not exist |

### test_agent_2_house.py (19 -> 19)

| case | K/R/D | what it protects now |
|---|---|---|
| HouseTurnTests.test_promise_recorded_through_the_tool_reaches_the_intent_book | R | renamed `test_agent_turn_reaches_the_intent_book_through_memory_extraction_not_a_tool`: an agent-backed resident's turn records nothing itself and a `record_promise` call is denied (D2); after a forced memory extraction with a fake model, the promise appears (`errands` shows milk, qty 1, for sora; the listing shows it open). Fails if the agent turn bypasses the memory path or the promise does not reach the kernel's errand list |

## Group g4: `test_p1_isolation_and_day.py`, `test_p1_onebot.py`, `test_review_batch_b_evidence.py`, `test_stage0_baseline.py`

Result: all four modules load and pass in full. No case was retired; every case that touched a promise guarantee still has one, rewritten onto the new path (promises made by memory extraction with a fake model, owner line in `owner_chat_queue`, closing by the kernel's evidence stream through `sweep`).

| file | before (this tree) | after |
|---|---|---|
| `test_p1_isolation_and_day.py` | 5 cases, 1 error | 5 cases, OK (K 4, R 1) |
| `test_p1_onebot.py` | 49 cases, 2 errors | 49 cases, OK (K 47, R 2) |
| `test_review_batch_b_evidence.py` | 5 cases, 1 failure, 2 errors | 5 cases, OK (K 2, R 3) |
| `test_stage0_baseline.py` | 13 cases, 1 failure, 3 errors | 13 cases, OK (K 9, R 4) |

No import fix was needed in any of the four files (none imported `worldkernel.intents_lite`); the failures were all calls to removed behaviour.

Common pattern used by the rewrites: the owner talks to a housemate, then `d.memory.extract(force=True, now=..., llm_fn=fake)` runs a fake extraction model that answers one `intent` op. The housemate's reply in these scenarios is a plain "好呀": the shared module counts her as agreed only for a plain yes (a reply such as "好的呀1" is not one), so the reply is part of the setup. Private promises are listed without their words (`listing` redacts), so items and quantities are read from `errands()` and refs from the `intents` table where a case needs them. Evidence rows written by hand carry `key` and `stream` inside the body, use the stream bound in `kernel_bind`, and a purchase receipt carries what the kernel's receipt carries (`intent_ref`, `account`, `beneficiary`, `fingerprint`, `action_id`, `resource_id`).

### `test_p1_isolation_and_day.py`

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| GroupGenerationDoesNotSeePrivateContext.test_private_chat_state_only_reaches_group_through_world_state_not_text | R | The privacy guarantee is kept: private words reach a group turn neither through the state card, nor through the promise listing, nor through a later group prompt. The promise is now made by extraction (D1, D8: no `house_intents`); positive control: the `intents` table holds exactly one `private` row with the marker words, and the listing shows that promise (placeholder text) so a listing that hid everything would fail. Dropped assertion: none of substance; the "和房主聊到" summary text the old detector wrote (D1) is no longer asserted absent in the sibling case `test_private_text_is_not_in_the_group_prompt_even_after_the_day_moves_on` (that case otherwise unchanged, K). The model for this case replies with a plain "好呀" so the shared module accepts the promise. |

### `test_p1_onebot.py`

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| IdentityTests.test_a_member_who_calls_themselves_owner_is_still_a_member | R | A group member who calls themselves 房主 (nickname, card, text) is stored and prompted as a member and gets no owner rights: after extraction `owner_chat_queue` stays empty. Positive control in the same scenario: the real bound owner then speaks in the same group, and after a second extraction exactly that line (and not the member's claim) is queued. Reads the table only if it exists (it appears with first use). |
| IdentityTests.test_a_bound_owner_in_a_group_is_the_owner_and_can_make_plans (renamed `..._is_the_owner`) | R | The identity half is kept: a bound owner in a group is stored and prompted as the owner, nothing is recorded by the chat turn itself, and after extraction the owner's group line reaches `owner_chat_queue` (`where: group`, with her words). "Can make plans" by rule-based recording (D1) is dropped from the name and the assertions. |

New module-level helpers: `queued_owner_lines`, `extract_with_a_promise`.

### `test_review_batch_b_evidence.py`

New helpers `_owner_asks`, `_add_evidence`, `_receipt`, `_states`, and a `_house` method; the loopback model server class that called `record_promise` is removed (D2).

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| test_b4_out_of_catalog_errand_stays_open_and_ignores_unrelated_purchases | R | Promises to buy things the shop does not sell (a durian, batteries) are not offered to the kernel as errands and are not closed by a purchase that belongs to another errand (an onion). Control in the same house: the egg errand is listed, and its own receipt closes the egg and only the egg. Dropped: the tool result `in_catalog` / note (D2), reminder rows with `raw_item` and `kind: errand` (D4, D1), `enqueue_chat` fallback (D1). The reply tail "不过「榴莲」我这边买不了…" is not tested here; it is covered by `test_realrun_1_promises.py` (the tail cases). |
| test_b5_private_promises_never_merge_across_residents | R | The same owner request made privately to two residents gives two promises (own id, own party, private), neither resident's receipt closes the other's promise, and a receipt made out to one errand but carrying the other resident's name closes nothing; control: each resident's own receipt closes hers. Dropped: `plan_ref` comparison and `plan_finished` closing (D1, D6). |
| test_b6_outing_departed_and_sleep_dialogue_never_false_close (renamed `test_b6_wrong_place_departure_and_sleep_dialogue_never_false_close`) | R | A departure alone, an arrival at the wrong place, and a script line about sleeping close nothing; the promise to sleep early (words the kernel cannot compile) is not closed even by a structured sleep activity. Control in the same scenario: a real arrival at the library closes the library promise. Dropped: the kinds `outing` / `chat_promise` and kind-based closing (D1, D6). Decided difference noted in the docstring: the old case expected a structured sleep activity to close the sleep promise; the shared rules leave words they cannot compile open until people say otherwise or time runs out. |

### `test_stage0_baseline.py`

New module-level helpers `_owner_asks`, `_errand_op`.

| case | K/R/D | what it protects now, or dropped item and where the equivalent is tested |
|---|---|---|
| Stage0ErrandIntentTests.test_tool_promise_with_empty_or_chinese_item_falls_back_and_completes_shopping (renamed `test_two_agreed_errands_are_carried_out_by_the_kernel_and_closed_by_its_receipts`) | R | Two agreed errands (3 onions, 2 eggs, the item read from the promise text) are listed as errands, carried out by the kernel in the daytime shopping window, and closed with the kernel's own evidence keys (`ai-house/...`, read from the stored refs). Dropped: the tool's item fallback for an empty or Chinese `item` (D2). |
| Stage0ErrandIntentTests.test_record_promise_tool_schema_and_allergy_note_not_completed_by_cooking (renamed `test_an_allergy_note_is_a_memory_that_cooking_does_not_touch`) | R | The standing allergy note is a memory entry, not a promise (no promise row exists); it is unchanged after she cooks dinner (control: a dinner was settled in the window). Dropped: the tool schema half (`record_promise` item description, D2) and the `cook` kind (D1); the note as a promise row is D3. |
| Stage0ErrandIntentTests.test_errand_not_closed_by_unrelated_arrival_and_handles_over_500_evidences | R | Same two guarantees. The errand is made by extraction, not `enqueue_chat` (D1). A park arrival and an egg purchase that belongs to no errand leave the "去超市买鸡蛋" errand open (it was open and listed in `errands`); then 505 unrelated rows, the arrival at the supermarket and the errand's own receipt close it, with exactly those keys. The old purchase row was hollow in the new code (rows outside the bound stream, or without the errand's reference, are never read), so the case now writes them the way the kernel does. The noise rows are written after the promise (the shared code reads evidence by time, so earlier rows would prove nothing). |
| Stage0ModelCompatTests.test_gpt5_compat_auto_adapts_test_llm_chat_and_memory_extractor | R | Purpose kept: every call to a model that refuses `max_tokens` / `temperature` is adapted (connection test, chat turn, memory extractor) and now also the life model call. It failed because the old chat turn queued the owner's line at once, which woke the life model (a third request); that call now happens only after the owner's line reaches the world at extraction time. The case runs that extraction, then asserts the life call exists and is adapted (checked by hand: without the extraction no life call is made, so the assertion is not vacuous). The `>= 2` count for the chat turn became `>= 1`. |

## Group 5: stage-2 backup/32h, commitments, turnkey v0.2 P3-P6, full day, review batch F

No case was retired. Counts (cases in file): before, after.

| file | before | after | result |
|---|---|---|---|
| `test_stage2_backup_and_32h.py` | 2 (1 error) | 2 | all pass |
| `test_stage2_commitments.py` | 2 (2 errors) | 2 | all pass |
| `test_turnkey_v02_p3_p6.py` | 9 (3 failures) | 9 | all pass |
| `test_v02_full_day_reproduce.py` | 1 (1 failure) | 1 | all pass |
| `test_review_batch_f_acceptance.py` | 2 (1 error) | 2 | all pass |

Support change outside the test files (allowed for this group only): `tools/run_stage2_32h_live.py`, the checker
`verify_stage2_32h_invariants` and the live runner's `main`; see "Checker" below.

Common method: a promise now exists only through memory extraction. Cases observe the owner's line and the
housemate's yes with `d.memory.observe(...)`, then run `d.memory.extract(force=True, ..., llm_fn=fake)` (or, in the
two cases that already ran a scripted/mock model, make that model answer the extraction with an `intent` op). Evidence
inserted by hand now names the kernel stream (`kernel_bind`), the intent (`intent_ref`), `account`, `action_id`,
`resource_id` and `beneficiary`, as the shared rules require.

### `test_stage2_backup_and_32h.py`

| case | K/R/D | what it protects now |
|---|---|---|
| Stage2BackupAnd32hTests.test_32h_virtual_time_stage2_contract_regression | R | Same whole-house 32 h regression. The mock model now answers the extraction with the promise op for the owner's onion errand (and says yes in chat); the errand is checked: absent after the chat turn alone, open with qty 2 after extraction, still open after "already bought" chat, completed by the kernel's real purchase within 32 h with an `ai-house/` evidence key. The allergy expectation is now a private memory (found, and still there after 32 h) and no promise row mentions it (D3). Dropped: `enqueue_chat` calls (D1), memo kind/state assertions (D3). The checker's last probe (allergy gone, was "memo closed") is now "allergy memory forgotten" and must give the `问题#2失败：未记录房主饮食过敏` message. Everything else (diaries, outfit log, usage, leak guard, group claim trust) unchanged. |
| Stage2BackupAnd32hTests.test_backup_verify_tamper_prune_restore_and_cli_http | K | unchanged, passes |

### `test_stage2_commitments.py`

| case | K/R/D | what it protects now |
|---|---|---|
| test_errand_verbal_claim_never_closes_until_real_evidence_arrives | R | Errand made by extraction (positive control: row exists, qty 2, open, with a day window). Stays open after a chat "bought it", after an unrelated purchase (another item, qty 2), and after a half-sized purchase; closes only when the second milk receipt completes qty 2; the errand reader then lists nothing. Dropped: `enqueue_chat`, kind `errand` (D1, D6). |
| test_allergy_memo_non_owner_gate_shared_plan_chatlink_expiry_and_cancel_api, renamed test_non_owner_gate_shared_plan_chatlink_quiet_expiry_and_cancel_501 | R | (1) Non-owner gate on the new path: a member's request, with the model naming either the owner or the member as beneficiary, creates nothing and queues nothing for ChatLink; the same batch from the owner with the same model answer creates the promise and queues one ChatLink row (positive control). (3) A joint plan of two housemates is one shared row (second housemate's batch merges, not copies), both agreed, and ChatLink shows it with the other party under "和谁"; done through `apply_chat` of the house's own module because a housemate's lines cannot be observed through the memory API. (4) `POST /api/intents/cancel` answers 501 `not_supported`, the list still shows the promise open. (5) Expiry by time: still open just after its window, expired long after, and nothing is written to the owner's queue (quiet expiry). Dropped: the allergy memo block (D3, 72 h "never closes/expires" is a memo guarantee, memory has none), `plan_ref` equality of two copies (D1), cancel 200 and `cancelled` (D7), `kind` assertions (D1). |

### `test_turnkey_v02_p3_p6.py`

| case | K/R/D | what it protects now |
|---|---|---|
| P3P6UnitTests.test_chat_agent_env_isolation_failover_and_tool_loop | R | Env isolation, failover to the fallback endpoint and the bounded tool loop (all six-minus-one tools, group `search_memory` positive/negative control) unchanged. Dropped: the `record_promise` tool call in the mock round (D2), the egg intent assertion (D2), the "member cannot trigger record_promise" count check (D2; the member's group turn is kept, it is what produces the group `search_memory` payload). Added: the tools offered to the model include `inspect_house` and not `record_promise`; the tool loop and reply record no promise. Non-owner gate on the new path: see commitments case above. |
| P3P6UnitTests.test_intents_errand_lifecycle_completes_on_evidence | R | Errand from extraction (nothing before it), listed open with qty 2, seen by `errands` with fingerprint/account/beneficiary, then closed by the kernel's own shopping within a daytime (`ai-house/` evidence key), after which `errands` is empty. Dropped: errand found by the chat turn (D1). |
| P3P6UnitTests.test_new_http_endpoints_and_token_tiers | R | Token tiers unchanged (diaries/memories/forget). `/api/intents`: 403 for the read token, 200 for the owner; a group promise is listed with text and item, a private-chat promise only as the placeholder (no item, no text: `鸡蛋` appears nowhere in the list); cancel is 403 for the read token and 501 `not_supported` for the owner and cancels nothing. |
| other 6 cases in the file | K | unchanged, pass |

### `test_v02_full_day_reproduce.py`

| case | K/R/D | what it protects now |
|---|---|---|
| FullDayTurnkeyV02Tests.test_24h_lifecycle_across_restart | R | Same continuous day and restart. The scripted model makes the errand through the extraction and says yes in chat; nothing exists before the extraction, one open egg promise after; a purchase receipt dated in the future does not close it before its time (positive control for the time window) and closes it after. Dropped: `house_intents` wording and chat-time recording (D1, D8). Restart, diary isolation, recall, leak guard unchanged. |

### `test_review_batch_f_acceptance.py`

| case | K/R/D | what it protects now |
|---|---|---|
| ReviewBatchFAcceptanceTests.test_verify_stage2_32h_invariants_negative_probes | R | The checker still reports: no sleep seen (#1), an open onion errand (#2), a leak in plain `group` (#4), and now a forgotten allergy memory (#2, replaces the "closed memo" probe) and a completed errand whose receipt is not a purchase (#2, new probe D). Each probe is preceded by the baseline returning `[]` (positive control). Seeds: allergy as a memory, onion errand by extraction (D1, D3, D8). |
| ReviewBatchFAcceptanceTests.test_real_dist_info_entry_points_tool_discovery_without_mocking_and_proxy_enforced | K | unchanged, passes |

### Checker (`tools/run_stage2_32h_live.py`)

The earlier edit already moved problem #2's diet check to memories and dropped the `kind` filter. Reviewed and kept; changes
made by this group: none to `verify_stage2_32h_invariants` itself; the live `main` already forced an extraction after the
three opening chats. Net effect checked by the negative probes: a missing diet memory is reported (the message text is
asserted), an open errand is reported, a completed errand with a non-purchase receipt is reported, a plain-`group` leak is
reported.

### Guarantees not covered elsewhere

- The memo guarantees "a diet note is never completed by cooking and never expires after 72 h" have no equivalent: a diet note is a
  memory now and memories have no completion or lifetime of that kind. Not tested after this change.
- Owner-only creation by rule (D1) is replaced by the shared module's checks (quote must be in the batch, said by a party or
  beneficiary by QQ number); the non-owner gate case above checks that on the house path. The conformance cases of the shared
  module (`intents-conformance-tests/`) cover the same checks at module level.
