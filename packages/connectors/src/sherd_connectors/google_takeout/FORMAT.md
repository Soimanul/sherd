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
`Meine Aktivitäten/Google Suche` are examples. Names are not used to classify data.

JSON activity arrays are recognized by `header`, `title`, `time`, and `products`.
HTML is recognized by `outer-cell` and `content-cell` classes. Chrome uses the
`Browser History` array. Detection samples at most five candidate files and reads
at most 64 KiB from each. Unrecognized files are ignored; ZIP members are opened
without extraction. A directory with many unrelated files ahead of its activity
files may need an explicit connector selection or a single-file import.

## Fields and canonical mapping

All rows go to `events`; source paths are relative to the export root (ZIP member
paths for archives). Identity hashes use kind, normalized UTC ISO timestamp,
URL (or title), and the zero-based occurrence among identical identities within
one file. File names and export dates are excluded. Identical activity exported
in JSON and HTML shares its identity; source-file provenance naturally differs.

| Export field | Canonical meaning |
| --- | --- |
| `header`, `products` / HTML header | YouTube → `youtube.watch`; Search → `google.search` |
| `time` / HTML timestamp | UTC `ts`; fractional ISO seconds retained |
| `title` / HTML action text | `title` without en/ro/de/es watch prefixes or en/ro/de search prefixes; German watch suffix removed |
| `titleUrl` / first action link | `url`; Search URL `q` parameter overrides the action query |
| `subtitles[0].name`, `.url` / following channel link | YouTube `meta.channel_name`, `meta.channel_url` |
| `details[].name` / content lines | YouTube `meta.from_ads`, recognizing en/ro/de/es/fr Google Ads labels |
| Chrome `time_usec` | UTC `ts`, integer microseconds since Unix epoch |
| Chrome `title`, `url` | `title`, `url`, kind `chrome.visit` |
| Chrome `page_transition` | `meta.page_transition` |

Removed YouTube videos remain events with NULL titles when their action title is
an HTTP URL or contains “a video that has been removed”. Search page-visit actions
(`Visited` and localised equivalents) are skipped and counted. Missing or malformed
required timestamps and malformed records are skipped; a single count is logged
at the end, without source content. Invalid JSON syntax fails the import rather
than silently truncating it. Unknown products are skipped and counted.

Only the listed fields are retained. Chrome `client_id` and `favicon_url`, other
subtitles/details, account/device/user fields, and extra source fields are dropped;
no raw records are stored. Channel names describe published content, not the
exporting account. Titles and URLs are the requested browsing activity itself.

## Dates and streaming

HTML month names and abbreviations cover English, Romanian, German, Spanish, and
French. German numeric `DD.MM.YYYY` dates, 12-hour AM/PM, and 24-hour times work.
Fixed timezone offsets are recognized for UTC, GMT, EET, EEST, CET, CEST, MEZ,
MESZ, BST, WET, WEST, EST, EDT, CST, CDT, MST, MDT, PST, and PDT. Unknown or missing
abbreviations use the import context's IANA zone. Ambiguous local times use fold 0;
nonexistent times shift forward across the DST gap. JSON activity must carry its
own timezone offset; Chrome is always UTC.

JSON arrays stream through `ijson`. HTML feeds an incremental UTF-8 decoder and
standard-library `HTMLParser` in 4 KiB chunks, retaining one activity cell at a time.
Occurrence counters grow with the number of distinct identities in each file.
No third-party Takeout parser or HPI code is used.

The eight synthetic golden variants cover English JSON/HTML YouTube, Romanian
HTML YouTube, German Search JSON, English Search HTML with visits, Chrome JSON,
a mixed ZIP, and removed/ad videos with malformed rows. The raw generator in
`synth_google_takeout.py` emits deterministic Chrome exports for memory checks.
