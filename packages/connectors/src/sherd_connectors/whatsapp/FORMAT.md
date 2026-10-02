# WhatsApp text exports

Connector id: `whatsapp`. Output table: `messages`.

## Getting an export

1. Open the conversation in WhatsApp on your phone.
2. On Android, open the chat menu, then **More → Export chat**. On iOS, open
   the contact/group details, then **Export Chat**.
3. Choose **Without media** for a smaller export or **Include media** for attachments.
4. Save the exported text or zip locally. Export each conversation you want to import.
5. Give sherd a `.txt`, `.zip`, or directory containing several exports. Set the
   timezone to the phone's local timezone and provide your own sender names/numbers
   as self identities. The export itself has neither timezone nor a self flag.

Android normally writes `WhatsApp Chat with <Name>.txt`. iOS writes `_chat.txt`,
usually inside `WhatsApp Chat - <Name>.zip`. Archives may also contain media;
only chat text is opened, with `zipfile`, without extracting anything to disk.
Directory imports include nested text exports and zip files in sorted chat-path order.
Detection walks paths lazily, sampling at most 64 KiB per candidate until a match;
it does not sort media folders or stop after three unrelated text files. A negative
directory probe still needs to visit every candidate, so cost scales with the directory.

## Supported text shapes

- Android: `<date>, <time> - <sender>: <text>`; system records have no sender.
- iOS: `[<date>, <time>] <sender>: <text>`; timestamps may include seconds.
- Continuations without a timestamp prefix join the previous text with `\n`.
- English US: `M/D/YY`, twelve-hour clock; English GB: `DD/MM/YYYY`, 24-hour clock;
  Romanian: `DD.MM.YYYY`, 24-hour clock; German: `DD.MM.YY`, 24-hour clock.
- AM/PM accepts uppercase/lowercase, `a.m.`/`p.m.` and `a. m.`/`p. m.`, ordinary spaces, U+00A0 and U+202F.
  Two-digit years mean 20xx. Across valid timestamp prefixes in a file, first field
  greater than 12 means D/M; second greater than 12 means M/D. Otherwise twelve-hour
  clocks mean M/D and 24-hour clocks mean D/M. D/M wins conflicting hints; rows invalid
  under that order are skipped. Mixing locale date orders in one chat is unsupported.
- A leading UTF-8 BOM and U+200E, U+200F, U+202A–U+202E, U+2066–U+2069 are removed.
  Emoji, including variation selectors, skin tones and joiners, remain intact.

Date order and chat-wide metadata use streaming passes before the row stream.
Only three distinct sender identities are retained for group detection. Occurrence
counts retain only hashes for the current timestamp, clearing when it changes. Memory
depends on the largest message and the current timestamp group, not export history.

## Field mapping

| Input | Canonical output |
| --- | --- |
| Android filename name; iOS archive name; otherwise first non-self sender | `chat_name`; whitespace-collapsed, NFC-normalized, casefolded name hashed with `whatsapp` → `chat_id` |
| More than two distinct non-system participants (self aliases count once), or group creation/membership/subject notice | `chat_kind='group'`; otherwise `direct` |
| Sender | `sender_name`; phone-looking values become `sender_id='whatsapp:+'` plus digits, otherwise `whatsapp:name:` plus the exported name |
| Sender matching a self identity | `is_from_me=True`; phone punctuation is ignored for matching |
| Local date/time | `ts`, localized with `ctx.tz`, then canonicalized to UTC; ambiguous times use `fold=0`, nonexistent times shift forward by the DST gap |
| Body and continuation lines | `text`, `kind='text'`; empty bodies are NULL |
| Omitted/attached media placeholder | `kind='media'`; `media_type` from explicit type or attachment extension; `text` is caption or NULL |
| Deleted placeholder | `kind='deleted'`, `text=NULL` |
| System body | `kind='system'`, `sender_id=NULL`, `sender_name=NULL`, `is_from_me=False`; the notice remains in `text` |
| Chat id, localized timestamp ISO string, sender, original body/placeholder, duplicate occurrence index starting at zero | `source_row_id=content_hash(...)`; file paths and export date are excluded |
| Path relative to export root | `source_file`; zip provenance is `<archive-relative-path>/<member>` |

Loose iOS chats with only self/system records have `chat_name=NULL`, `chat_kind=direct`,
and `chat_id=content_hash("whatsapp", "unnamed", first_valid_record_UTC_ts.isoformat(),
first_valid_record_body_or_placeholder)`. This preserves notes-to-self and system-only
chats without inventing a name; a count of unnamed chats is logged. Their id remains
stable when later messages are appended, but removing the first message changes it.

`reply_to_id`, `contact_id` and `meta` remain NULL. Exports do not supply reply metadata.
Attachments are not opened, decoded, copied or put in `meta`. No device/account
metadata, IP addresses, usernames or email metadata are collected. Sender names and
message bodies are the user-requested canonical message fields; logs contain neither.

Media placeholders: `<Media omitted>`, `image/video/audio/sticker/GIF/document omitted`,
`<attached: filename>`, `filename (file attached)`, `<Media lipsă>`, `<Fișier media omis>`,
`<Medien ausgeschlossen>` and `<Multimedia omitido>`. Photo extensions become `image`,
video extensions `video`, audio extensions `audio`, `.webp`/STICKER names `sticker`,
`.gif` `gif`, other named extensions `document`, and generic omitted media `other`.
Captions on the same or subsequent lines are retained; whitespace-only captions are NULL.

Deleted notices include the English "This message was deleted" and "You deleted this
message", Romanian "Acest mesaj a fost șters" and "Ai șters acest mesaj", German
"Diese Nachricht wurde gelöscht" and "Du hast diese Nachricht gelöscht", Spanish
"Este mensaje fue eliminado", "Eliminaste este mensaje" and "Borraste este mensaje".
Deleted phrases accept one optional trailing period in every supported language.
System records cover encryption, group creation/membership/subject/icon/description
changes, missed calls and security-code changes. Timestamped senderless records are
always system. Sender-prefixed records are user messages unless the text begins with
U+200E and the entire cleaned text matches a known system notice. The marker is checked
before stripping controls; matching never searches sender names or arbitrary user prose.
An empty `Sender:` is accepted like `Sender: `, retaining its sender and NULL text.

Malformed timestamp records with a recognized prefix (invalid date/clock), or an
empty sender, are skipped. One count-only warning is emitted after parse completes.
A parse producing zero rows emits a count-only warning.
Lines without a recognized timestamp are continuations; preamble lines are ignored.
A continuation beginning with an entire valid export timestamp is indistinguishable
from a new record. A timestamp embedded within prose remains text. Duplicate
occurrence indices restart per source chat and whenever the timestamp changes. Repeated
identical records in the same timestamp block retain distinct identities. If a timestamp
reappears after another timestamp, identical records can share an id and collapse. This
coordinator-approved tradeoff bounds bookkeeping for chronological export streams.

## Fixtures and acceptance evidence

Eight synthetic variants: `android-en-us`, `android-en-gb-group`, `android-ro`,
`android-de`, `ios-en` (zip plus synthetic media), `ios-ro-group`,
`multiline-and-unicode`, `edge-cases`. Each is below 200 KB. Goldens were generated
with `SHERD_UPDATE_GOLDENS=1` and each resulting JSONL row was reviewed.

| Acceptance | Tests in `tests/whatsapp/test_whatsapp.py` (plus shared harness) |
| --- | --- |
| Registration, all eight goldens | Shared `test_golden`, `test_every_connector_ships_fixtures` |
| Detection, confidence, directory/file/zip, unrelated fixtures | `test_detect_variants_and_confidence`, `test_detect_negative_and_generic_text`, `test_detect_negative_other_connectors`, `test_detect_negative_other_formats`, `test_detect_reads_only_first_64_kb`, `test_directory_and_zip_without_extraction` |
| Android/iOS, four locales, seconds, deterministic raw generator | `test_synth_four_locales`, `test_android_name_and_zip_name` |
| Date-order detection, ambiguous cases and late hints | `test_date_order_detection` |
| Continuations, emoji, bidi marks, BOM, AM/PM variants | `test_multiline_unicode_and_clock_variants` |
| Media placeholders, types, empty/inline/multiline captions; NULL reply metadata | `test_media_types_and_captions` |
| Localized deleted messages | `test_deleted_locales` |
| System notices and user text containing system phrases | `test_system_lines`, `test_system_phrases_in_user_text_are_text` |
| Chat names, group detection, sender ids, self identities | `test_sender_and_chat_identity`, `test_unnamed_notes_to_self_identity`, `test_unnamed_system_only_chat`, `test_group_system_messages`, `test_group_sender_count_and_late_evidence`, `test_android_name_and_zip_name` |
| DST gaps/folds, identical messages in one minute, stable content ids | `test_dst_gap_and_fold`, `test_identical_messages_have_occurrence_ids`, `test_occurrence_reset_on_timestamp_change` |
| Malformed rows, count-only logging | `test_malformed_rows_skipped_and_count_only_log`, `test_empty_sender_is_skipped` |
| Re-import/overlap | `test_run_import_idempotent` (all variants), `test_overlapping_export_import` |
| At least 50 MiB, RSS below 200 MiB | `test_streaming_50_mb` using shared `assert_streaming`, with short distinct records (<200 bytes per line) |

Raw generator: `python -m sherd_connectors.whatsapp.synth_whatsapp PATH --locale en-US
--bytes 52428800 --seed 4` (also `en-GB`, `ro-RO`, `de-DE`, and `--ios`).

Negative detection exercises synthetic Spotify/shell/bank/Takeout shapes and every
other integrated connector fixture; it asserts that cross-connector candidates exist.

## Identity limitations

`source_row_id` hashes the localized timestamp ISO string, as contracted. Importing
the same export with a different timezone changes the interpreted instant and its row
id; always reuse the export timezone. Unnamed chat ids instead hash the first record's
UTC timestamp. These formulas deliberately remain unchanged in connector version 2.
Loose iOS files use the first non-self sender as the name rather than extracting the
subject from notices: `ios-ro-group` is named `Mira Example`, not `Luni de hârtie`.
Starting a later export at a different participant can therefore change its chat id
and prevent overlap deduplication. Removing the first record of an unnamed chat also
changes its id. Prefer named Android files or named iOS archives for stable chat names.
Self aliases count as one participant for group inference but retain separate exported
sender ids; cross-source entity resolution owns sender identity unification.

## Fix-round evidence

| Review finding | Regression evidence |
| --- | --- |
| 1: user security-code prose loses sender | `test_review_user_phrases_keep_sender` |
| 2: marked iOS notices and deletion periods | `test_review_marked_ios_notices`, `test_deleted_locales`, `ios-ro-group` golden |
| 3: sender names ending in left/containing added | `test_review_user_phrases_keep_sender` |
| 4: spaced AM/PM, zero-record warning | `test_review_spaced_ampm_is_new_record`, `test_review_zero_records_count_only_warning` |
| 5: localized hash vs UTC unnamed hash risk | `test_review_localized_timestamp_hash_contract`, `test_unnamed_notes_to_self_identity`; formulas documented above |
| 6: loose-iOS chat identity risk | `test_review_loose_ios_identity_depends_on_first_sender`; limitation documented above |
| 7: empty sender-prefixed body | `test_review_empty_user_body` |
| 8: eager directory discovery and three-file cutoff | `test_review_detection_after_three_nonchat_files`, `test_review_detection_stops_before_remaining_directory`, `test_detect_reads_only_first_64_kb` |
| 9: self aliases distort fixture group inference | `test_review_self_aliases_do_not_make_direct_chat_group`; corrected synthetic self phone and affected goldens |
| 10: trivial detection reason/vacuous other-connectors test | `test_detect_variants_and_confidence`, `test_detect_negative_other_connectors`; exact reason and nonempty cross-connector candidates |

The earlier 500 MiB benchmark parsed 3,443,506 rows in 128.33 seconds at 68.58 MiB
peak RSS (macOS, Python 3.12.14, Europe/Bucharest). Fix-round `scripts/check` passed: format, lint, strict types, PII scan and all
740 tests in 47.45 seconds, including `test_streaming_50_mb`, with no skips and one
expected existing network-guard warning. All fixtures are synthetic; real-device exports remain unverified.
