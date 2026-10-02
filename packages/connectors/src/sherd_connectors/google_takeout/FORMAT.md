# Google Takeout activity

## Get the export

1. Open Google Takeout at `https://takeout.google.com/` and deselect all products.
2. Select **YouTube and YouTube Music**, **My Activity**, and/or **Chrome**.
3. In their data-selection dialogs select YouTube history, Search activity, and
   Chrome browsing history. Choose JSON where offered; HTML activity is also supported.
4. Create a one-time ZIP export, download it, and import the ZIP directly or its
   extracted directory. A single JSON or HTML history file also works.

These instructions describe the export UI; this connector makes no network requests.

## Layout and recognition

Common paths are `YouTube and YouTube Music/history/watch-history.json` (or `.html`),
`My Activity/Search/MyActivity.json` (or `.html`), and `Chrome/History.json` or
`BrowserHistory.json`. Names can be localized: `Activitatea mea/Căutare` and
`Meine Aktivitäten/Google Suche` are examples. Names only prioritize detection
samples; contents classify data.

JSON activity arrays are recognized by `header`, `title`, `time`, and `products`.
HTML is recognized by `outer-cell` and `content-cell` classes. Chrome uses the
`Browser History` array. Detection prioritizes filenames hinting at activity/history
first, then product paths, then other files. It samples up to 32 files and reads
at most 64 KiB from each. Unrecognized files are ignored; ZIP members are opened
without extraction. A directory with many unrelated files ahead of its activity
files may need an explicit connector selection or a single-file import.

## Fields and canonical mapping

All rows go to `events`; source paths are relative to the export root (ZIP member
paths for archives). Identity hashes use kind, normalized UTC ISO timestamp,
URL (or title), and the zero-based occurrence among identical identities within
the current timestamp run in one file. Counters reset when the timestamp changes
in parse order. Non-adjacent identical records at the same timestamp may collapse
if intervening records change the timestamp; this is accepted. File names and
export dates are excluded. Identical activity exported in JSON and HTML shares
its identity; source-file provenance naturally differs.

| Export field | Canonical meaning |
| --- | --- |
| `header`, `products` / HTML header | YouTube watch actions → `youtube.watch`; YouTube search actions → `youtube.search`; Search → `google.search` |
| `time` / HTML timestamp | UTC `ts`; fractional ISO seconds retained |
| `title` / HTML action text | `title` without en/ro/de/es watch prefixes or en/ro/de/es search prefixes; German watch suffix removed |
| `titleUrl` / first action link | `url`; Search URL `q` parameter overrides the action query |
| `subtitles[0].name`, `.url` / following channel link | YouTube watch `meta.channel_name`, `meta.channel_url` |
| `details[].name` / body and caption lines | YouTube watch `meta.from_ads`, recognizing en/ro/de/es/fr Google Ads labels |
| Chrome `time_usec` | UTC `ts`, integer microseconds since Unix epoch |
| Chrome `title`, `url` | `title`, `url`, kind `chrome.visit` |
| Chrome `page_transition` | `meta.page_transition` |

Removed YouTube videos remain events with NULL titles when their action title is
an HTTP URL or contains “a video that has been removed”. Search page-visit actions
(`Visited` and localised equivalents) are skipped and counted. Missing or malformed
required timestamps and malformed records are skipped. Separate visited,
unsupported-product/action, and malformed counts are logged at the end,
without source content. Invalid JSON syntax fails the import rather
than silently truncating it. Unknown products and unsupported YouTube actions
are skipped and counted.
YouTube searches retain the query from action text and the `titleUrl`; Google
Search alone uses the URL `q` parameter to override the query.

Only the listed fields are retained. Chrome `client_id` and `favicon_url`, other
subtitles/details, account/device/user fields, and extra source fields are dropped;
no raw records are stored. Channel names describe published content, not the
exporting account. Titles and URLs are the requested browsing activity itself.

## Dates and streaming

HTML month names and abbreviations cover English, Romanian, German, Spanish, and
French. German numeric `DD.MM.YYYY` dates, 12-hour AM/PM (including Spanish
`a. m.`/`p. m.`), and 24-hour times work.
Unicode whitespace is normalized before action-prefix matching and date parsing.
Fixed timezone offsets are recognized for UTC, GMT, EET, EEST, CET, CEST, MEZ,
MESZ, BST, WET, WEST, EST, EDT, CST, CDT, MST, MDT, PST, and PDT; numeric GMT/UTC
offsets such as `GMT+2` also work. Unknown or missing abbreviations use the import context's IANA zone. Ambiguous local times use fold 0;
nonexistent times shift forward across the DST gap. JSON activity with no offset
is interpreted as UTC, as are UTC exports; Chrome is always UTC.

JSON arrays stream through `ijson`. HTML feeds an incremental UTF-8 decoder and
standard-library `HTMLParser` in 4 KiB chunks, retaining one outer activity entry
at a time. Its body supplies action text, links, and timestamp; other cells
supply product/detail text. Exactly one record
is emitted when the outer entry closes. Occurrence counters retain identities
only for the current timestamp, not the whole file.
No third-party Takeout parser or HPI code is used.

The nine synthetic golden variants cover English JSON/HTML YouTube, Romanian
HTML YouTube, German Search JSON, English Search HTML with visits, Chrome JSON,
a mixed ZIP, removed/ad videos with malformed rows, and a multi-cell HTML Ads
entry. All HTML fixtures use multi-cell entries with separate product captions.
The raw generator in `synth_google_takeout.py` emits deterministic short Chrome
records with distinct timestamps and URLs for memory checks (roughly 100 bytes
per record).

Manual streaming check (macOS arm64, Python 3.12.14, 2026-10-02): a 524,288,036-byte
(500 MiB) file containing 5,542,213 short Chrome records parsed in 33.69 seconds
at 69.45 MiB peak RSS in a fresh process (including startup); generation took
6.26 seconds. The 50 MiB regression test uses `assert_streaming` with a 100 MiB
RSS limit and checks that its sample contains short records.
The same 50 MiB input measured 69.88 MiB RSS with the fixed counter and
141.94 MiB with the reset removed, confirming the test catches that regression.
