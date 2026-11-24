# M1 切换：kit 原有记忆用例逐个处理的清单

配 `docs/PORTING-PLAN.md` 15.16 看。小屋的记忆库换成与私有部署共用的那一份代码、原来 kit 自己的实现删除之后，
原来对着旧实现写的用例分给五组分别处理，每个用例三选一：

- **K 保留**：原样通过（或只改了导入行），查的东西仍然是真的。
- **R 改写**：用例保护的那条保证还在，改成经由新库去查同一条保证。
- **D 删除**：所测的东西已经不存在（旧表结构、旧迁移、旧查询语句）。

规矩：不跳过、不标“预期失败”、不为了通过而放松断言；“不应该出现”的断言必须在同一场景里有一个“该出现的地方确实出现了”的对照；
看着像真缺陷的不许绕过去，留着失败并上报。

下面各组的表是处理人交回的原文（英文），只去掉了交回时的提问一节——那些提问的处置写在 15.16 里。交回之后又动过的三处：

- 第二组那个故意留着失败的用例（字和字之间加空格复述私聊记忆）：公共代码已修，用例改成“同一句话用五种分隔写法都被拦，另有一句无关的话加了空格也不被拦”，现在通过。
- 第一组、第四组里“还没整理的私聊原话”的标签：公共代码改成免查的群里写“想提可以自然地提”，相应的三个用例改为断言这一句（并断言没有“别说出来”那一句）。
- 迁移工具对“停在很早版本的旧库”补了一步（15.16），新增两个用例，不在下面的表里。

## 第一组：`test_private_memory_groups.py`

Result: 80 cases, all pass (`Ran 80 tests ... OK`, ~20 s). Before: 80 cases, import failure; after fixing imports 22 did not pass.
Counts: K 55, R 24, D 1 (the deleted one is replaced by net-zero new cases; two cases were added, one removed again as redundant, so the total stays 80).

Import fix: `JST`, `NO_MEMORY_NOTE`, `PRIVATE_IN_GROUP_NOTE_FREE/HUSH` are no longer exported; the file now defines them locally (wording copied from the shared module's `_private_note` / `NO_MEMORY_NOTE`). New helper `db_rows` (direct read of memory-common.db, used only where storage is the thing checked), `PENDING_MARK`, and `make(extra_env=)`.

### Cases that changed

| case | K/R/D | what it protects now / why |
|---|---|---|
| DefaultIsUnchanged.test_read_sql_is_byte_identical_to_the_old_one | D | asserted the old `_visible_sql` text; no such method. Behaviour is covered by the next case |
| DefaultIsUnchanged.test_group_recall_never_shows_private_memory | R | added positive control (private chat finds it) |
| ReadInListedGroup.test_she_sees_her_own_private_memory_marked_as_such | R | mark is now "[事件·私密 关于房主]" (was "[私密·事件]") |
| ReadInListedGroup.test_switch_one_reads_in_any_group_and_the_note_tone_follows_switch_two | R | same mark change |
| ReadInListedGroup.test_all_kinds_and_trust_levels_are_returned | R | builds the store with AI_HOUSE_MEMORY_TRUST=1; two kinds/trust levels; mark change |
| ReadInListedGroup.test_without_switch_one_nothing_is_read_even_when_switch_two_lists_the_group | R | positive control added |
| ReadInListedGroup.test_the_note_appears_only_when_a_private_row_was_really_brought_out | R | mark change; positive control (note + mark do appear for the private question) |
| ReadInListedGroup.test_list_entries_stays_exact_scope | R | positive control (private list has the row) |
| PrivateRowsCarryOneMarkNotTwo.test_the_label_is_not_doubled | R | mark change |
| RecallHelpers.test_what_the_owner_just_said_and_is_not_extracted_yet_is_known_in_the_group | R | pending line is now marked "房主私聊·私密：你知道，但别在群里说出来"; no separate note line for it |
| RecallHelpers.test_pending_lines_of_another_roommate_and_questions_are_not_used | R | positive controls (her own line shown; yuki's line shown to yuki) |
| RecallHelpers.test_the_fallback_needs_switch_one | R | positive control (private chat gets the row) |
| WriteAccess.test_a_listed_group_cannot_rewrite_or_void_a_private_memory | R | `_peek_writable` is gone: allow_shared must not open update/invalidate from the group; control: same id updates from the private scope |
| WriteAccess.test_memories_formed_in_a_listed_group_are_still_that_groups_group_memories | R | reads `seen` (chat group:1 / private:1, private flag) instead of the old table |
| UnifiedGroupFence.test_listed_group_memories_do_not_show_in_an_unlisted_group | R | N3: renamed `..._are_not_fenced_under_unified_recall`; asserts they DO show in unlisted groups, control in the listed group |
| UnifiedGroupFence.test_shared_group_rows_are_channel_bound_so_they_cannot_leave_either | R | N3: renamed `..._follow_unified_recall_too`; shared row of a listed group shows elsewhere |
| UnifiedGroupFence.test_without_unified_groups_channels_were_already_separate | R | positive control added |
| UnifiedGroupFence.test_a_private_memory_never_rides_along_with_unified_recall | new | unified recall without switch one never brings private rows into any group (with control) |
| UnifiedGroupFence.test_world_scope_is_read_everywhere_so_nothing_from_a_group_is_written_there | R | storage check: seen has only chat group:1, world list empty |
| SharedOrWorld... (3 cases + helper) | R | the op is `"scope": "public"`; rows read through list_entries (owner "shared" / "nell"); also asserts `added == 1` so the op really ran |
| DaemonWebGroup.test_listed_group_prompt_carries_it_and_the_retelling_is_delivered | R | pending mark replaces "[私密·" + note |
| DaemonWebGroup.test_listing_only_a_qq_group_leaves_the_web_group_hushed | R | pending mark; still blocked |
| DaemonWebGroup.test_four_combinations_in_the_web_group | R | also adds an extracted memory (the note tone belongs to memory rows, not to pending lines); markers chosen so the question cannot satisfy the assertion |
| DaemonWebGroup.test_a_reply_that_leaks_a_secret_is_still_held_back_in_a_listed_group | R | was vacuous: now first shows the plain retelling IS delivered in the same scenario, then retelling + internal path is blocked, and the logged reason is `internal_path` |
| DaemonWebGroup.test_group_memories_formed_there_do_not_reach_other_rooms | R | was vacuous: positive controls added (group row visible in its group by list/recall, private row found in the web group, world memory found by world recall), then the other-room / world negatives |
| DaemonWebGroup.test_search_memory_tool_follows_the_same_rule | R | mark change; control that the private chat's search finds it in both subtests |
| QqGroupEnd..test_listed_qq_group_gets_the_retelling, test_listing_another_qq_group_hushes_this_one, test_only_switch_one_knows_but_cannot_say | R | pending mark instead of "[私密·" / note |
| QqGroupEnd..test_only_switch_two_exempts_but_does_not_make_her_know | R | control: the owner's private line is stored (`seen`, private=1) |

All other cases are K (config parsing, malformed ids, CLI validation, ReplyGuard, outbound checks, send gate, proactive, direct QQ delivery, etc.). N11: all outbound cases use REPEAT, which repeats most of the memory, and pass unchanged; they all contain the blocked/not-blocked pair in the same scenario.

Deleted: 1 (the SQL text case). No case was deleted for N3; both fence cases were rewritten.

## 第二组：`test_p2_memory.py`、`test_review_batch_b_evidence.py`、`test_stage2_memory_evidence.py`

### test_p2_memory.py (13 old cases, 14 now)

| case | K/R/D | what it protects now / why |
|---|---|---|
| test_fake_model_extraction_add_update_and_relative_dates | R | Add and update ops in the shared format. Relative dates show as 明天〔即11月22日〕 in the prompt and 明天（11月22日） in recall. The earlier wording is kept in `mem_ver`. The store is built with `AI_HOUSE_MEMORY_EVIDENCE=1`, because date notes belong to the evidence layer, which is off by default. |
| test_default_keyword_ranking_zero_dependency (was `..._keyword_and_recency_...`) | R | An unrelated question gives "". The better keyword match ranks first, checked both ways. `uses` is counted. The "equal match, newer first" assertion is removed (see Q1). |
| test_optional_vector_plugin_semantic_recall_and_graceful_fallback | K | Unchanged, passes. |
| test_private_memories_never_leak_to_group_world_other_actors_or_extractor | R | See the next paragraph. |
| test_spaced_out_repeat_of_a_private_memory_is_still_stopped_by_the_outbound_check (new, split out) | R, FAILING | The old assertion, unchanged, with a positive control (the plain repeat is blocked). See D1. |
| test_private_extraction_cannot_write_private_text_into_group_memory_via_update | R | A private extraction cannot reword a group row (1 rejected, 1 added). Group recall stays clean and private recall has the surprise. The old `assertNotIn("#1", prompt)` is dropped (see Q4). |
| test_invalidation_and_ttl_expiration_stop_recall_immediately | R | See the second paragraph below the table. |
| test_persistence_across_restart_and_refusal_isolation | R | The file is `memory-common.db`. Memory and pending lines survive a restart. A secret-bearing line and injection text are refused. Refusal split recovery works. I added a control that another housemate cannot read the restarted memory. |
| test_repeated_migrations_from_legacy_schema_and_narrow_api (now `test_module_level_narrow_api_and_late_night_date_guard`) | R | The schema-migration half (`migrate()`, `SCHEMA_VERSION`, the v1 table) is deleted with the old schema. Kept: module-level `configure/observe/extract/recall` and `narrow_api()`. `annotate_relative_dates` is gone, so its 02:00 guard is checked through the extraction prompt (noon line gets the date note, 02:15 line does not, plain line present as control). |
| test_transient_extractor_failure_keeps_seen_pending_and_backs_off | R | A failure leaves the line undone, the backoff skips the model, and the retry after the backoff works. The `seen` column is `consumer` now. |
| test_member_multiline_spoofing_and_invalid_scope_channel_rejected | R | `seen` has one rendered `line`. A member cannot get the owner's card, the owner's number (1) or a newline into the header. The real owner line shows `[房主\|1]` as control. The four ValueError checks are unchanged. |
| test_duplicate_add_idempotency_and_max_active_archiving | K | Unchanged, passes. |
| test_http_openai_embedder_opt_in_via_env_loopback_mock | K | Unchanged, passes. |
| test_vector_backfill_for_rows_created_before_plugin_enabled | K | Unchanged, passes. |

The privacy case (`test_private_memories_never_leak_to_group_world_other_actors_or_extractor`) now checks:
- Private memory does not reach group, world or another housemate, with the positive controls kept.
- A group batch cannot `forget` or `update` private #1. That gives 2 rejected and 1 added. The old "invalidate" op is now "forget".
- An `add` with `scope:"private"` is only an ordinary group memory (listed as group, not private).
- Private #1 is intact.
- The outbound check stops a repeat of the private memory and of a pending private line. New control: an unrelated reply passes.

The TTL case (`test_invalidation_and_ttl_expiration_stop_recall_immediately`) now checks:
- Invalidate removes the memory from recall, search and list. The row status is now `deleted`; the returned dict still says `invalidated`.
- An expired row is excluded by a plain read with no explicit sweep, and its status is `expired`.
- A second TTL memory proves a not-yet-due one is still found, and that `sweep_expired()` returns 1 for a memory nobody read.
- Reads sweep by themselves, so the first explicit `sweep_expired()` call returns 0.

Counts: K 4, R 9, plus one new split-out case that fails on purpose. No whole case was deleted, only the migration half of one case.

### test_review_batch_b_evidence.py (6 old cases, 5 now)

| case | K/R/D | what it protects now / why |
|---|---|---|
| b1_group_owner_evidence_binding... | R | The store is built with `AI_HOUSE_MEMORY_TRUST=1`. Same four ops plus one more (2b). An op with no src becomes a claim. An op citing src [1] but taking a member's words becomes a claim (two variants). An op whose words match the owner line becomes `verified`, source `auto:owner_verified`. A group rule without owner words is rejected. Now 4 added and 1 rejected; the rows are listed and counted, and the rejected rule is checked absent. |
| b2_conflict_suppression... | R | TRUST on. The old verified fact outranks the fresh conflicting claim (`_conflict_verified`, score ratio above 1.5). Recall shows the verified fact and keeps the claim out (asserted). The wording check through the private `_format_row` is gone. |
| b3_schema_v3_migration... | D | It tests the old `schema_migrations` v3 migration on a hand-built old `memory.db` (`BASE_TABLES_SQL`, `SCHEMA_VERSION`). Nothing like it exists in the new store (see Q2). |
| b4, b5, b6 (intents) | K | These are daemon intent tests, not store tests. Only the import line changed. They pass. |

Counts: K 3, R 2, D 1.

### test_stage2_memory_evidence.py (3 cases)

| case | K/R/D | what it protects now / why |
|---|---|---|
| pure_question_plus_self_guess_is_rejected_by_evidence_gate | R | TRUST on. All three ops are rejected and nothing is listed. I added a positive control in the same scenario: a member's statement with the same op shape is stored as `group_stated`. |
| group_member_claim_downgraded_and_ranked_below_verified | R | TRUST on. Downgrade to claim, ranking below verified, and "[群聊陈述·未核实" in recall. Only the env changed. |
| owner_claim_stays_verified_and_repeated_private_allergy_leak_blocked (renamed) | R | An owner statement in a group stays `verified`. A repeat of most of a pending private allergy line is stopped. An unrelated reply is not. |

One part of the third case is deleted: the "item plus allergy word" paraphrase (点菜注意别放虾仁，可能会过敏). It is one of the three N11 rules that were not carried.

Counts: R 3.

## 第三组：`test_tail_2_memory_intents.py`、`test_tail_9_private_memory_evidence.py`

Both files load and pass (37 cases, run twice). Edited only `test_tail_2_memory_intents.py` and `test_tail_9_private_memory_evidence.py`.

Finding: the earlier table (written at an older commit) is outdated. Trust levels, the evidence check on extraction (`_owner_lines_support`, `_op_trust`), relay detection and conflict flags are now in the shared module, behind `MEMORY_TRUST` (= `AI_HOUSE_MEMORY_TRUST=1`). So nearly everything was rewritten, not deleted. Both files now build the store with `env={"AI_HOUSE_MEMORY_TRUST": "1"}`.

Non-vacuity check: I ran both files with trust OFF. 39 subtests failed, covering every trust/evidence/conflict case. The "must not" cases are therefore not passing for the wrong reason, and each has a positive control in the same scenario.

### test_tail_2_memory_intents.py (32 cases -> 30)

| case | K/R/D | what it protects now |
|---|---|---|
| BuyRequestParsing x4 | K | intents, untouched (import line only) |
| EvidenceWindowAndStrength x12 | K | intents, untouched |
| MemoryTrust.test_member_update_cannot_rewrite_verified_fact | R | a member's update op does not rewrite a verified memory; it lands beside it as a claim. Reads via `list_entries`, channel `qq-group:5001` |
| MemoryTrust.test_member_update_without_src_cannot_rewrite_verified_fact | R | same, no `src` |
| MemoryTrust.test_owner_update_still_rewrites_verified_fact | R | the owner can still rewrite (positive control) |
| MemoryTrust.test_member_update_of_unverified_claim_is_still_allowed | R | a claim can be updated by a member |
| MemoryTrust.test_add_merge_cannot_overwrite_verified_text_with_weaker_trust | R | dedupe-merge does not lower a verified text |
| MemoryTrust.test_add_merge_still_merges_equal_or_stronger_trust | R | merge of an equal level still merges |
| MemoryTrust.test_owner_relaying_a_third_party_is_a_claim_marked_as_relayed | R | a relayed line gives a claim/group_stated with "relay" in source |
| MemoryTrust.test_owner_first_hand_statements_stay_verified | R | first-hand lines stay verified, source `auto:owner_verified` |
| MemoryConflicts x6 | R | `search` flags a conflicting claim with `_conflict_verified`; cross-person and agreeing claims are not flagged |
| MigrationLogging.test_v3_migration_logs_demoted_row_count_once_and_is_idempotent | D | old `memory.db` schema v3 migration, built from `BASE_TABLES_SQL`. The old schema is gone. Carry-over of old houses is `ai_house.memory_migrate` (its own tests: `test_m1_migrate.py`) |
| MigrationLogging.test_fresh_store_logs_no_demotion | D | "no demotion log at start" has no counterpart: nothing is demoted at start any more |

Counts: K 16, R 14, D 2. (Of the 14 R: 8 MemoryTrust cases + 6 MemoryConflicts cases; the relay and first-hand cases use subTests.)

Logger question you asked about: the new store logs to `ai_house.memory_common` (and the shared module to `memory`). `ai_house.memory` still has a `log` object. After deleting both logging cases, no `assertLogs` is left in the file. The `logging` import was removed.

### test_tail_9_private_memory_evidence.py (7 cases -> 7)

All R: same cases, same assertions, with the store switched to `memory_common` and trust on. `JST` is defined locally (no longer in `ai_house.memory`).

| case | K/R/D | what it protects now |
|---|---|---|
| fabricated_fact_citing_unrelated_owner_line | R | a fact the owner did not say is stored as a claim "推断·未核实", not verified |
| fabricated_variants | R | same for other fabrications, different allergen, different date |
| paraphrases_of_the_cited_line_stay_verified | R | paraphrases and added dates stay verified |
| negation_flip | R | a negation flip is downgraded, the same polarity is verified |
| multi_line_src_pools_cited_lines_only | R | only the cited lines count |
| item_citing_only_roommate_own_line_is_still_rejected | R | citing only her own line is rejected (`rejected` 1). I added a positive control: the same text citing the owner's line is stored |
| group_scope_is_unchanged | R | group owner first-hand is verified, a member's statement is claim/group_stated "未核实" |

Counts: K 0, R 7, D 0. Nothing deleted.

## 第四组：`test_realrun_5_trust.py`、`test_realrun_2_memory_diary.py`、`test_realqq_1_listed_group_recall.py`、`test_realrun_6_diary_retry.py`

All four files load and pass in full. Only the four assigned files were edited. No `src/` change, no git state change.

### Counts
| file | before | K | R | D | new | after |
|---|---|---|---|---|---|---|
| test_realrun_5_trust.py | 21 | 15 | 6 | 0 | +2 | 23 |
| test_realrun_2_memory_diary.py | 25 | 14 | 11 | 0 | 0 | 25 |
| test_realqq_1_listed_group_recall.py | 5 | 2 | 3 | 0 | +1 | 6 |
| test_realrun_6_diary_retry.py | 33 | 32 | 1 | 0 | 0 | 33 |

Nothing deleted. (K = import fixed only, or `env=` trust switch added; R = changed question/assertions.)

### test_realrun_5_trust.py
Import: `JST` defined locally, `_owner_lines_support` import removed. Every store built with `env={"AI_HOUSE_MEMORY_TRUST": "1"}`.
| case | | note |
|---|---|---|
| test_nell_rows_of_every_wording_are_verified | R | question now asks about what the row is about (allergy vs birthday); new recall needs shared content words |
| test_yuki_secret_rows_of_every_wording_are_verified | K | |
| test_claim_without_about_or_src_is_verified_when_the_line_backs_it | K | |
| test_the_leading_date_is_only_free_when_it_is_the_day_the_line_was_said | K | |
| test_support_check_directly (was MannerWords) | R | helper gone; same rule checked through private `extract`: owner line backs the text -> verified |
| test_manner_words_alone_back_nothing | R | through extract; added positive control (same line backs the real content) |
| test_negation_in_a_confidentiality_request... | R | through extract (verified vs downgraded) |
| allergen / birthday / month / explicit month / different shop (5) | K | |
| claim about somebody else; relayed line (2) | K | |
| test_the_residents_own_guess_is_not_owner_words | R | second half: new store rejects (added 0, rejected 1) instead of keeping a downgraded claim; added positive control (owner says it himself -> verified) |
| hearsay about third party not verified | K | |
| group scope (3) | K | |
| rementions (2) | K | merge raises trust, not kind; still true |
| NEW test_default_is_off_no_labels_and_claims_read_as_plain_memories | new | pins default: no env -> a `claim` op is stored as fact, no trust label, no 未核实/推断 in recall (control: memory is found) |
| NEW test_switch_on_labels_an_unbacked_claim | new | same scenario with the switch on shows 推断·未核实 |

### test_realrun_2_memory_diary.py
Import: `JST` local; `TRUST_ENV` used by DirectQuestionRecall and the four PrivateSecretTrust stores (trust is off by default).
- DirectQuestionRecall (3): K with env; added positive controls to "unrelated questions" and "group never reads private" (R for those two).
- UnknownThingProbe (5): K; added positive controls to "same note with/without private rows" and "unrelated private memory" (2 R).
- PrivateSecretTrust (4): K with env; "month contradicts" strengthened: added==1 and trust == group_stated (R).
- DiaryNotCutMidSentence (10), DiaryPromptStaysOnTheList (3): K (diary service; `test_stored_in_memory_in_full` really reads the diary through `list_entries`).
(counts above: R = 2+2+1 + the 3 env-only cases counted with the trust class as R = 11 loosely; none changed what is protected.)

### test_realqq_1_listed_group_recall.py
Import: the notes `NO_MEMORY_NOTE`, `PRIVATE_IN_GROUP_NOTE_FREE/HUSH` are no longer exported by `ai_house.memory`; copied as literals from the shared module.
- listed group gets coffee-shop line: R. A not yet extracted private line is now shown with its own label ("房主私聊·私密：你知道，但别在群里说出来") also in the listed group; the FREE note is not there (see question 1).
- unlisted group: R, asserts the new label.
- NEW: stored private memory (via `add`) gives the FREE note in the listed group and the HUSH note in the unlisted one: this keeps the old note guarantee.
- plain "no" for never-told question: K. Switch off: R (positive control with the switch on). Brain prompt text: K.

### test_realrun_6_diary_retry.py
Loads and passes. `MemoryStore` import moved to the top. `test_memory_gets_the_diary_once` is real (diary kind row listed through `list_entries("world")`); strengthened to check the row holds the retry's real diary text. All other cases are about the diary service / router, not the store.

## 第五组：经整栋小屋跑的 8 个文件

All 8 modules pass together: 47 tests, OK. Only my 8 test files were edited. No D (deletes). No defects found.

| file | case | K/R/D | what it protects now |
|---|---|---|---|
| test_stage1_identity | test_identity_store_resolves_cross_channel... | K | identity store |
| | test_memory_seen_speaker_identity_daemon_api_and_onebot_bot_filter (renamed) | R | owner chat and bound QQ owner are recorded in `seen` as owner (header `|1] (message_id=`); bot speakers add no line (positive control: lines exist before). The "legacy memory.db without speaker_id gets ALTERed" part was deleted (old schema migration, no longer exists). |
| test_stage2_backup_and_32h | test_backup_verify_tamper_prune_restore_and_cli_http | R | backup lists `memory-common.db`; restored house recalls the memory |
| | test_32h_virtual_time_stage2_contract_regression | R | mock extractor now matched on "记忆整理员" (new prompt); house built with `AI_HOUSE_MEMORY_TRUST=1` (case is about claim/group_stated); leaking reply now repeats the memory, positive control = private recall finds the allergy memory |
| test_stage3_image_gen | one case | R | memory changed to 钥匙 (not 密码); prompt repeats the memory; positive control private recall |
| test_stage3_voice | one case | R | text repeats the memory "房主的隐私忌口：海胆过敏"; positive control private recall |
| test_stage3_web_search | one case | R | query repeats the memory; positive control private recall |
| test_tail_5_daemon_reset_config | test_add_resident_does_not_drop_the_inflight_reply | R | reads `memory-common.db` `seen` (column `consumer`), asserts reply line and the user line |
| | other 29 cases | K | |
| test_turnkey_v02_p3_p6 | test_chat_agent_env_isolation_failover_and_tool_loop | R | memory is "房主的私人保险柜位置是个秘密（不能泄露）" (银行卡/密码 are refused by the filter); positive control: the private-chat search_memory payload contains it, the group one does not |
| | test_new_http_endpoints_and_token_tiers | R | diary text long enough; positive control added before the forget (private recall finds 蜂蜜) |
| | test_diary_service_evening_generation_isolation_and_recall | R | recall question "日记写了什么"; the private line is made a real memory by a scripted extract and is recalled in private (positive control for the "diary must not contain it" checks) |
| | other 6 cases | K | |
| test_v02_full_day_reproduce | test_24h_lifecycle_across_restart | R | extractor matched on "记忆整理员", ops in the new format; leaking reply repeats the memory; group diary recall question shares content words with the (live-model) diary text |

Counts: K = 1 + 29 + 6 = 36 cases, R = 11 cases, D = 0 (one sub-part of the stage1 case deleted).

### Findings on the specific questions
- Voice and web search: verified directly on the store. `check_outbound_leak` gives [] for "其实房主对海胆过敏" (item + allergy word rule) and for "青苹果核桃" (key phrase rule): both are exactly the two rules that were not carried (N11). The memories themselves store fine. With a reply/query that repeats the memory the check returns the id, and the pipeline blocks. No defect.
- Image: failure was only the secret filter (密码 in the stored memory); same fix.
- Diary recall that came back empty (turnkey p3p6 and full-day): the diary IS stored and visible (world scope and group scope both find it). The question "昨晚日记写了什么" shares only the one bigram 日记 with it, below the content-word threshold; "日记写了什么" or "昨天的日记" find it. In the full-day case the live-model diary text contains no 日记 word at all, so the question had to share real words (灯火吃茶馆). Not a defect (decided scoring difference).
- Diary too short: `ValueError: 日记太短` in the http endpoints case, fixed with a longer diary.

