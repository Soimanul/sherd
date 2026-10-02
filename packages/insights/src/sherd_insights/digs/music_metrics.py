"""Music digs: SQL aggregates over local calendars, deterministic stories."""

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline


def _selection(params: DigParams) -> tuple[str, list[object]]:
    if params.top_n < 1:
        raise ValueError("top_n must be at least 1")
    if params.granularity not in ("day", "week", "month", "year"):
        raise ValueError("unknown granularity")
    return (
        """WITH history AS (
        SELECT *, coalesce(artist, 'Unknown') AS artist_label,
            coalesce(track, 'Unknown') AS track_label, timezone(?, ts) AS local_ts
        FROM media_plays WHERE media_kind IN ('track', 'episode')
    ), selected AS (
        SELECT * FROM history WHERE (?::DATE IS NULL OR local_ts::DATE >= ?::DATE)
        AND (?::DATE IS NULL OR local_ts::DATE <= ?::DATE)
    ) """,
        [params.tz, params.date_from, params.date_from, params.date_to, params.date_to],
    )


def _empty(data: pa.Table) -> DigResult:
    return DigResult(
        data,
        None,
        "No listening to explore in this range yet.",
        None,
        "No chart: there are no matching plays.",
    )


def _line(data: pa.Table, value: str, title: str, *, percent: bool = False) -> dict[str, Any]:
    chart = charts.line(data, "bucket", value)
    chart["mark"] = {"type": "line", "point": True}
    chart["encoding"]["x"]["title"] = "Local date"
    chart["encoding"]["y"]["title"] = title
    if percent:
        chart["encoding"]["y"]["axis"] = {"format": ".0%"}
    return chart


@dataclass
class MusicDig:
    id: str
    title: str
    requires: list[str] = field(default_factory=lambda: ["media_plays"])

    def compute(self, store: Store, params: DigParams) -> DigResult:
        prefix, args = _selection(params)
        metric = self.id.removeprefix("music.")
        if metric == "listening_minutes":
            data = store.query(
                prefix
                + """SELECT date_trunc(?, local_ts)::DATE AS bucket,
                sum(ms_played) / 60000.0 AS minutes FROM selected
                GROUP BY bucket ORDER BY bucket""",
                [*args, params.granularity],
            )
            if not data.num_rows:
                return _empty(data)
            hours = sum(float(r["minutes"]) for r in data.to_pylist()) / 60
            return DigResult(
                data,
                _line(data, "minutes", "Listening minutes"),
                f"You listened for {hours:.1f} hours in this range.",
                Headline("Listening time", round(hours, 1), "hours"),
                f"Listening minutes by local {params.granularity}; tracks and episodes included.",
            )
        if metric == "top_artists_by_year":
            limit = min(params.top_n, 5)
            data = store.query(
                prefix
                + """, totals AS (
                SELECT year(local_ts) AS year, artist_label AS artist,
                    sum(ms_played) / 60000.0 AS minutes
                FROM selected WHERE media_kind = 'track' GROUP BY year, artist_label
            ), ranked AS (SELECT *, row_number() OVER
                (PARTITION BY year ORDER BY minutes DESC, artist) AS rank FROM totals)
            SELECT * FROM ranked WHERE rank <= ? ORDER BY year, rank""",
                [*args, limit],
            )
            if not data.num_rows:
                return _empty(data)
            tracks = store.query(
                prefix
                + """, totals AS (
                SELECT year(local_ts) AS year, artist_label AS artist, track_label AS track,
                    sum(ms_played) / 60000.0 AS minutes
                FROM selected WHERE media_kind = 'track' GROUP BY year, artist_label, track_label
            ), ranked AS (SELECT *, row_number() OVER
                (PARTITION BY year ORDER BY minutes DESC, artist, track) AS rank FROM totals)
            SELECT * FROM ranked WHERE rank <= ? ORDER BY year, rank""",
                [*args, limit],
            )
            rows = data.to_pylist()
            data = data.append_column(
                "top_tracks",
                pa.array(
                    [
                        [
                            {k: v for k, v in track.items() if k != "year"}
                            for track in tracks.to_pylist()
                            if track["year"] == row["year"]
                        ]
                        for row in rows
                    ]
                ),
            )
            chart = charts.bar(rows, "artist", "minutes")
            # A facet owns its categories; otherwise each panel reserves all years' artists.
            unit = {k: chart[k] for k in ("mark", "encoding", "width", "height")}
            unit["height"] = max(140, limit * 28)
            unit["encoding"]["y"]["sort"] = {"field": "minutes", "order": "descending"}
            chart = charts.apply_theme(
                {
                    "data": {"values": charts.records(rows)},
                    "facet": {"row": {"field": "year", "type": "ordinal", "title": None}},
                    "spec": unit,
                    "resolve": {"scale": {"y": "independent"}},
                }
            )
            latest = next(row for row in rows if row["year"] == rows[-1]["year"])
            return DigResult(
                data,
                chart,
                f"Your top artist in {latest['year']} was {latest['artist']} "
                f"with {latest['minutes']:.0f} minutes.",
                Headline("Latest year's top artist", str(latest["artist"])),
                "Top track artists by listening minutes in each local year; top tracks in data.",
            )
        if metric == "discovery_rate":
            data = store.query(
                prefix
                + """, first_plays AS (
                SELECT artist_label, date_trunc('month', min(local_ts))::DATE AS first_month
                FROM history GROUP BY artist_label)
            SELECT date_trunc('month', s.local_ts)::DATE AS bucket, count(*) AS plays,
                count(*) FILTER (WHERE f.first_month =
                    date_trunc('month', s.local_ts)) AS new_plays,
                new_plays::DOUBLE / plays AS discovery_rate
            FROM selected s JOIN first_plays f USING (artist_label)
            GROUP BY bucket ORDER BY bucket""",
                args,
            )
            if not data.num_rows:
                return _empty(data)
            rate = sum(float(r["discovery_rate"]) for r in data.to_pylist()) / data.num_rows
            return DigResult(
                data,
                _line(data, "discovery_rate", "Discovery share", percent=True),
                f"On average, {rate:.1%} of your monthly plays came from artists "
                "you first heard that month, using your whole listening history.",
                Headline("Average monthly discovery", round(rate * 100, 1), "%"),
                "Monthly share of plays from artists first heard that local month in all history.",
            )
        if metric == "seasonality":
            data = store.query(
                prefix
                + """, tagged AS (
                SELECT artist_label AS artist, ms_played,
                    CASE WHEN month(local_ts) IN (12,1,2) THEN 'winter'
                         WHEN month(local_ts) IN (6,7,8) THEN 'summer' END AS season,
                    CASE WHEN hour(local_ts) BETWEEN 5 AND 10 THEN 'morning'
                         WHEN hour(local_ts) >= 22 OR hour(local_ts) <= 3 THEN 'night' END AS time
                FROM selected
            ), periods AS (
                SELECT artist, ms_played, 'season' AS comparison, season AS period
                FROM tagged WHERE season IS NOT NULL UNION ALL
                SELECT artist, ms_played, 'time' AS comparison, time AS period
                FROM tagged WHERE time IS NOT NULL
            ), totals AS (
                SELECT comparison, period, artist, sum(ms_played) AS total_ms
                FROM periods GROUP BY comparison, period, artist
            ), shares AS (SELECT comparison, period, artist, total_ms / 60000.0 AS minutes,
                total_ms::DOUBLE / nullif(sum(total_ms) OVER
                (PARTITION BY comparison, period), 0) AS share FROM totals)
            SELECT * FROM shares ORDER BY comparison, period, minutes DESC, artist""",
                args,
            )
            if not data.num_rows:
                return _empty(data)
            rows = data.to_pylist()
            panels = []
            for comparison, periods in (
                ("season", ["winter", "summer"]),
                ("time", ["morning", "night"]),
            ):
                candidates = [row for row in rows if row["comparison"] == comparison]
                artists = list(
                    dict.fromkeys(
                        row["artist"]
                        for period in periods
                        for row in candidates
                        if row["period"] == period
                    )
                )
                keep = {
                    row["artist"]
                    for period in periods
                    for row in [r for r in candidates if r["period"] == period][: params.top_n]
                }
                values = [
                    {
                        "artist": artist,
                        "period": period,
                        "share": next(
                            (
                                r["share"]
                                for r in candidates
                                if r["artist"] == artist and r["period"] == period
                            ),
                            0.0,
                        ),
                    }
                    for artist in artists
                    if artist in keep
                    for period in periods
                ]
                panel = charts.bar(
                    values, "artist", "share", series="period", paired=True, series_order=periods
                )
                panel["encoding"]["x"]["axis"] = {"format": ".0%"}
                panel["encoding"]["x"]["title"] = "Share of listening minutes"
                panel["title"] = (
                    "Winter vs summer" if comparison == "season" else "Morning vs night"
                )
                panels.append({k: v for k, v in panel.items() if k not in ("$schema", "config")})
            seasons = [r for r in rows if r["comparison"] == "season"]
            differences = {
                artist: sum(
                    (r["share"] or 0) * (1 if r["period"] == "winter" else -1)
                    for r in seasons
                    if r["artist"] == artist
                )
                for artist in sorted({r["artist"] for r in seasons})
            }
            winner = (
                max(differences, key=lambda artist: abs(differences[artist]))
                if differences
                else None
            )
            narrative = (
                f"Your biggest seasonal shift was {winner}: "
                f"{abs(differences[winner]):.1%} of listening share between winter and summer."
                if winner
                else "You have morning or night listening, but no winter or summer plays."
            )
            return DigResult(
                data,
                charts.apply_theme(
                    {"vconcat": panels, "resolve": {"scale": {"color": "independent"}}}
                ),
                narrative,
                Headline("Biggest seasonal shift", winner or "No seasonal plays"),
                "Artist shares of listening minutes: December-February vs June-August, "
                "and local 05:00-10:59 vs 22:00-03:59. Missing artist shares are zero.",
            )
        if metric == "skips_obsessions":
            data = store.query(
                prefix
                + """SELECT date_trunc('month', local_ts)::DATE AS bucket,
                count(skipped) AS known_plays, count(*) FILTER (WHERE skipped) AS skips,
                skips::DOUBLE / nullif(known_plays, 0) AS skip_rate
                FROM selected GROUP BY bucket ORDER BY bucket""",
                args,
            )
            if not data.num_rows:
                return _empty(data)
            obsessions = store.query(
                prefix
                + """SELECT artist_label AS artist, track_label AS track,
                date_trunc('week', local_ts)::DATE AS week, count(*) AS plays,
                date_trunc('month', min(local_ts))::DATE AS bucket
                FROM selected WHERE media_kind = 'track' GROUP BY artist_label, track_label, week
                HAVING count(*) > 30 ORDER BY plays DESC, week, artist, track""",
                args,
            ).to_pylist()
            data = data.append_column(
                "obsessions",
                pa.array(
                    [
                        [
                            {k: v for k, v in r.items() if k != "bucket"}
                            for r in obsessions
                            if r["bucket"] == bucket
                        ]
                        for bucket in data["bucket"].to_pylist()
                    ],
                    type=pa.list_(
                        pa.struct(
                            [
                                ("artist", pa.string()),
                                ("track", pa.string()),
                                ("week", pa.date32()),
                                ("plays", pa.int64()),
                            ]
                        )
                    ),
                ),
            )
            if obsessions:
                biggest = obsessions[0]
                description = f"{biggest['track']} by {biggest['artist']}"
                narrative = (
                    f"You played {description} {biggest['plays']} times in the local ISO week "
                    f"starting {biggest['week']}."
                )
                headline = Headline(
                    f"{description} · week of {biggest['week']}", int(biggest["plays"]), "plays"
                )
            else:
                headline = Headline("Biggest obsession", "No track over 30 plays in a week")
                narrative = "No track passed 30 plays in a local ISO week in this range."
            return DigResult(
                data,
                _line(data, "skip_rate", "Skip rate", percent=True),
                narrative,
                headline,
                "Monthly skip share among plays with a known skipped flag; "
                "tracks played more than 30 times in one local ISO week are listed in data.",
            )
        raise ValueError(f"unknown music metric: {metric}")


DIGS: list[Dig] = [
    MusicDig("music." + metric, title)
    for metric, title in (
        ("listening_minutes", "Listening time"),
        ("top_artists_by_year", "Top artists by year"),
        ("discovery_rate", "New artist discovery"),
        ("seasonality", "Seasonal listening"),
        ("skips_obsessions", "Skips and obsessions"),
    )
]
