# Spotify streaming history

## Get your export

1. Sign in to your Spotify account in a browser.
2. Open the account privacy settings and find **Download your data**.
3. Request **Extended streaming history** for the most complete listening history.
   **Account data** includes a shorter, less detailed streaming history and is also supported.
4. Complete Spotify's email confirmation, wait for the export-ready email, and download the ZIP.
5. Import the ZIP, its unpacked directory, or an individual history JSON file with `sherd dig`.
   Requesting/downloading happens outside sherd; this connector makes no network calls.

Import extended history first when you also have account data. Store imports use **first write
wins**: importing account data first retains its minute-resolution timestamps and missing metadata,
even when extended history is imported later. Within one directory/ZIP, extended files are always
processed first; files within each format are sorted by relative path.

## Layout and known variants

Files contain a JSON array of play objects. Unrelated files are ignored. Supported basenames:

- `Spotify Extended Streaming History/Streaming_History_Audio_<years>_<n>.json`
- `Spotify Extended Streaming History/Streaming_History_Video_<years>.json`
- `MyData/StreamingHistory_music_<n>.json`
- `MyData/StreamingHistory_podcast_<n>.json`
- Older account exports: `MyData/StreamingHistory<n>.json`

The containing directories may differ. ZIP members are streamed with `zipfile`, never extracted.
For ZIPs, provenance is `<zip-relative-path>/<member-path>`; for unpacked exports it is the file
path relative to the import context's export root. Detection reads at most three 64 KiB prefixes:
recognized names plus history fields score 0.99; a recognized array layout alone scores 0.6;
unrecognized or unreadable exports score 0.

Golden variants are `extended-audio` (two overlapping files, repeated plays, episodes, audiobooks),
`extended-video` (kind precedence), `extended-old-fields` (old identifying fields, nulls, missing
required fields), `account-music`, `account-podcast`, `account-legacy-name`, and
`overlap-cross-format-extended` / `overlap-cross-format-account` (the same repeated music
plays and episode, tested together through the store).

## Mapping to `media_plays`

All exported timestamps are UTC, independent of the import timezone. Offset-bearing timestamps
are normalized to UTC. Timestamp seconds are preserved in the canonical row; only identity uses
minute precision. Missing/null optional fields remain null. Missing, invalid, or negative durations,
missing/invalid timestamps, and non-object items are skipped; the total count is logged once after
parsing, without record content. Zero durations and integral numeric durations (including `1000.0`) are valid. Invalid JSON syntax fails the import.

| Extended field | Canonical mapping |
| --- | --- |
| `ts` | `ts`, play end timestamp in UTC |
| `ms_played` | `ms_played`, integer milliseconds |
| `platform` | `platform` |
| `master_metadata_album_artist_name`, `episode_show_name`, `audiobook_title` | First nonempty value → `artist` |
| `master_metadata_track_name`, `episode_name`, `audiobook_chapter_title` | First nonempty value → `track` |
| `master_metadata_album_album_name` | `album` |
| `spotify_track_uri`, `spotify_episode_uri`, `audiobook_chapter_uri` | First nonempty value → `uri` |
| `shuffle`, `skipped` | Boolean columns; null stays null, never inferred |
| `reason_start`, `reason_end`, `offline`, `incognito_mode`, `conn_country` | Only these five keys go into `meta`; absent or structured values are null, non-integer numbers are strings |
| `audiobook_title`, `audiobook_uri`, `audiobook_chapter_uri`, `audiobook_chapter_title` | Any nonempty value selects `media_kind=audiobook` |
| `episode_name`, `episode_show_name`, `spotify_episode_uri` | Any nonempty value selects `episode`, unless audiobook fields apply |

After audiobook and episode checks, a video filename selects `video`; otherwise the kind is `track`.
Rows lacking both title and URI are kept with `track=null`, including local-file plays.
`audiobook_uri` is used only for kind detection; the chapter URI is the canonical playable URI.

| Account field | Canonical mapping |
| --- | --- |
| `endTime` (`YYYY-MM-DD HH:MM`) | `ts`, UTC |
| `msPlayed` | `ms_played` |
| `artistName`, `podcastName` | First nonempty value → `artist` |
| `trackName`, `episodeName` | First nonempty value → `track` |
| Podcast filename, `podcastName`, `episodeName` | Select `episode`; otherwise `track` |

Account rows have null `album`, `uri`, `platform`, `shuffle`, `skipped`, and `meta`.

## Privacy and identity

`ip_addr`, `username`, and `user_agent_decrypted` are dropped entirely: they identify the account
or device and serve no canonical listening field. `offline_timestamp` and all unrecognized fields
are also dropped. No raw sink receives source objects, and no record content is logged.

Both formats use
`content_hash(UTC minute ISO timestamp, media_kind, artist, track, ms_played, occurrence_index)`.
Occurrence indices start at zero in each file and count identical identity fields only while
processing the current exact UTC timestamp. The counter clears whenever the timestamp changes
in parse order. Identical records separated by another timestamp may therefore collapse into
one stored row, even if their timestamps are equal; this is accepted. Different seconds in the
same minute can also share an identity. Spotify supplies no source play ID to resolve this.

All valid records are emitted, including overlaps. Across files and formats matching IDs collapse
in the store, with the first record retained; the import ledger counts duplicates as seen but
not inserted. Identity excludes filename, URI, album and export date, aligning richer extended
records with account rows. Repeated plays split across files can lose their occurrence correspondence.

JSON arrays stream through `ijson`. Only the current timestamp's identity counters are retained
in memory; there is no disk-backed bookkeeping or export-wide seen set. Memory does not grow
with the number of distinct timestamps. Within a single timestamp it scales with distinct identities.

## Synthetic generator

`SpotifyGenerator` implements the framework's `RawGenerator.write(path, approx_bytes, seed)`
contract. It writes deterministic extended audio history directly to disk with short records, unique play minutes and distinct track identities:

```sh
uv run python -m sherd_connectors.spotify.synth_spotify \
  /tmp/Streaming_History_Audio_2024_0.json --bytes 524288000 --seed 5
```

The streaming test generates at least 50 MiB and enforces peak RSS under 200 MiB in a fresh process.
