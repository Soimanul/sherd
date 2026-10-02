"""Cross-source digs, aggregating facts before comparing local calendars."""

from dataclasses import dataclass, field, replace
from datetime import timedelta
from math import isfinite
from typing import Any

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline
from sherd_insights.digs.money_metrics import SPEND, _currency_note
from sherd_insights.digs.money_metrics import _selection as money_selection
from sherd_insights.digs.music_metrics import _selection as music_selection

FACTS = ("messages", "media_plays", "transactions", "events", "locations")


def fact_tables(store: Store) -> list[str]:
    """Inspect metadata, without counting or assuming every fact table exists."""
    present = {
        row["table_name"]
        for row in store.query(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' "
            "AND database_name = current_database()"
        ).to_pylist()
    }
    return [table for table in FACTS if table in present]


def _empty(data: pa.Table) -> DigResult:
    return DigResult(
        data,
        None,
        "No matching sherds in this range yet.",
        None,
        "No chart: there is no matching activity.",
    )


def _range(params: DigParams) -> tuple[str, list[object]]:
    return (
        "(?::DATE IS NULL OR local_ts::DATE >= ?::DATE) "
        "AND (?::DATE IS NULL OR local_ts::DATE <= ?::DATE)",
        [params.date_from, params.date_from, params.date_to, params.date_to],
    )


def _timeline(store: Store, params: DigParams) -> DigResult:
    if params.granularity not in ("day", "week", "month", "year"):
        raise ValueError("unknown granularity")
    parts: list[str] = []
    args: list[object] = []
    where, range_args = _range(params)
    for table in fact_tables(store):
        stream = "split_part(kind, '.', 1)" if table == "events" else "?"
        parts.append(
            f"SELECT local_ts::DATE AS day, {stream} AS stream, count(*) AS activity "
            f"FROM (SELECT *, timezone(?, ts) AS local_ts FROM {table}) "
            f"WHERE {where} GROUP BY day, stream"
        )
        if table != "events":
            args.append({"media_plays": "plays"}.get(table, table))
        args.extend([params.tz, *range_args])
    if not parts:
        return _empty(
            pa.table(
                {
                    "bucket": pa.array([], type=pa.date32()),
                    "stream": pa.array([], type=pa.string()),
                    "activity": pa.array([], type=pa.int64()),
                }
            )
        )
    data = store.query(
        "WITH days AS ("
        + " UNION ALL ".join(parts)
        + """), busiest AS (
            SELECT day AS busiest_day, sum(activity)::BIGINT AS busiest_activity
            FROM days GROUP BY day ORDER BY busiest_activity DESC, day LIMIT 1
        ), buckets AS (
            SELECT date_trunc(?, day)::DATE AS bucket, stream,
                sum(activity)::BIGINT AS activity FROM days GROUP BY bucket, stream
        ) SELECT *, 100.0 * activity / max(activity) OVER (PARTITION BY stream)
            AS activity_pct, (bucket + CASE ? WHEN 'day' THEN INTERVAL 1 DAY
            WHEN 'week' THEN INTERVAL 7 DAY WHEN 'month' THEN INTERVAL 1 MONTH
            ELSE INTERVAL 1 YEAR END)::DATE AS bucket_end
            FROM buckets CROSS JOIN busiest ORDER BY bucket, stream""",
        [*args, params.granularity, params.granularity],
    )
    if not data.num_rows:
        return _empty(data)
    rows = data.to_pylist()
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["stream"]] = totals.get(row["stream"], 0) + row["activity"]
    streams = sorted(totals, key=lambda stream: (-totals[stream], stream))[:8]
    # One band per stream; all bands share the same time scale, with explicit interval ends.
    chart = charts.heatmap(
        [r for r in rows if r["stream"] in streams], "bucket", "stream", "activity_pct"
    )
    chart["encoding"]["x"] = {"field": "bucket", "type": "temporal", "title": "Local date"}
    chart["encoding"]["x2"] = {"field": "bucket_end"}
    chart["encoding"]["y"]["sort"] = streams
    chart["encoding"]["y"]["title"] = None
    chart["encoding"]["color"]["title"] = "share of the stream's busiest period"
    chart["encoding"]["color"]["scale"] = {"domain": [0, 100]}
    chart["encoding"]["color"]["legend"] = {"orient": "bottom", "titleLimit": 360}
    chart["encoding"]["tooltip"] = [
        {"field": "bucket", "type": "temporal"},
        {"field": "stream"},
        {"field": "activity"},
    ]
    chart["params"] = [
        {"name": "zoom", "select": {"type": "interval", "encodings": ["x"]}, "bind": "scales"}
    ]
    chart["height"] = max(160, len(streams) * 36)
    day, activity = rows[0]["busiest_day"], rows[0]["busiest_activity"]
    return DigResult(
        data,
        chart,
        f"Your busiest local day was {day}, with {activity} activities.",
        Headline(f"Busiest day · {day}", activity, "activities"),
        f"Activity counts by local {params.granularity} for {', '.join(streams)}. "
        f"Busiest day across all streams: {day}, {activity} activities. "
        f"Showing {len(streams)} of {len(totals)} streams.",
    )


def _soundtrack(store: Store, params: DigParams) -> DigResult:
    # A supplied endpoint anchors the missing bound; otherwise use the latest track day.
    end = params.date_to
    if params.date_from is None and end is None:
        end = store.query(
            "SELECT max(timezone(?, ts)::DATE) AS day FROM media_plays WHERE media_kind = 'track'",
            [params.tz],
        ).to_pylist()[0]["day"]
    start = params.date_from or (end - timedelta(days=29) if end else None)
    selected = replace(params, date_from=start, date_to=end)
    prefix, args = music_selection(selected)
    data = store.query(
        prefix
        + """SELECT artist_label AS artist, track_label AS track,
        track_label || ' · ' || artist_label AS label, count(*) AS plays,
        sum(ms_played) / 60000.0 AS minutes FROM selected WHERE media_kind = 'track'
        GROUP BY artist_label, track_label ORDER BY plays DESC, minutes DESC, artist, track
        LIMIT ?""",
        [*args, params.top_n],
    )
    if not data.num_rows:
        return _empty(data)
    winner = data.to_pylist()[0]
    period = f"{start or 'the beginning of your data'} to {end or 'the end of your data'}"
    chart = charts.bar(data, "label", "plays")
    chart["encoding"]["x"]["title"] = "Plays"
    chart["encoding"]["y"]["title"] = None
    chart["encoding"]["tooltip"] = [{"field": "label"}, {"field": "plays"}, {"field": "minutes"}]
    return DigResult(
        data,
        chart,
        f"From {period}, your #1 track was {winner['track']} by "
        f"{winner['artist']}, with {winner['plays']} plays.",
        Headline("Top track", str(winner["label"])),
        f"Top tracks from {period}, ranked by plays, then listening minutes.",
    )


def _correlation(store: Store, params: DigParams, *, money: bool) -> DigResult:
    where, args = _range(params)
    currency: str | None = None
    if money:
        prefix, selection_args, currency = money_selection(store, params)
        query = (
            prefix
            + f""", x AS (
            SELECT date_trunc('week', local_ts)::DATE AS week, sum({SPEND})::DOUBLE AS x
            FROM selected GROUP BY week
        ), local_y AS (SELECT *, timezone(?, ts) AS local_ts FROM media_plays
            WHERE media_kind IN ('track', 'episode')),
        y AS (SELECT date_trunc('week', local_ts)::DATE AS week,
            sum(ms_played) / 60000.0 AS y FROM local_y WHERE {where} GROUP BY week)"""
        )
        query_args = [*selection_args, params.tz, *args]
        x_name, y_name = "spend", "minutes"
        x_label, y_label = f"Spend ({currency})", "Listening minutes"
    else:
        query = f"""WITH local_x AS (
            SELECT timezone(?, ts) AS local_ts FROM messages WHERE is_from_me AND kind != 'system'
        ), x AS (SELECT date_trunc('week', local_ts)::DATE AS week, count(*)::DOUBLE AS x
            FROM local_x WHERE {where} GROUP BY week),
        local_y AS (SELECT timezone(?, ts) AS local_ts FROM events WHERE kind = 'youtube.watch'),
        y AS (SELECT date_trunc('week', local_ts)::DATE AS week, count(*)::DOUBLE AS y
            FROM local_y WHERE {where} GROUP BY week)"""
        query_args = [params.tz, *args, params.tz, *args]
        x_name, y_name = "messages", "youtube_watches"
        x_label, y_label = "Messages sent", "YouTube watch events"
    # Use the union of observed weeks: a missing stream is zero, never a dropped observation.
    # If a stream has no data at all, there is no comparison to make.
    data = store.query(
        query
        + f""", paired AS (
        SELECT week, coalesce(x, 0) AS {x_name}, coalesce(y, 0) AS {y_name}
        FROM x FULL OUTER JOIN y USING (week)
        WHERE EXISTS (SELECT 1 FROM x) AND EXISTS (SELECT 1 FROM y)
    ) SELECT *, corr({x_name}, {y_name}) OVER () AS r, count(*) OVER () AS n_weeks
        FROM paired ORDER BY week""",
        query_args,
    )
    if not data.num_rows:
        return _empty(data)
    raw_r = data.to_pylist()[0]["r"]
    # Parallel SQL reduction order can differ at machine precision.
    r = round(float(raw_r), 12) if raw_r is not None and isfinite(raw_r) else None
    # DuckDB returns NaN for zero variance; JSON/Arrow output must use null.
    data = data.set_column(
        data.schema.get_field_index("r"), "r", pa.array([r] * data.num_rows, type=pa.float64())
    )
    n = data.num_rows
    description = (
        "no clear relationship"
        if r is None or abs(r) < 0.2
        else ("a positive relationship" if r > 0 else "a negative relationship")
    )
    note = _currency_note(currency, params) if money and currency else ""
    statistic = f"r = {r:.2f}" if r is not None else "r is undefined (insufficient variation)"
    chart: dict[str, Any] = charts.apply_theme(
        {
            "data": {"values": charts.records(data)},
            "width": 480,
            "height": 240,
            "encoding": {
                "x": {"field": x_name, "type": "quantitative", "title": x_label},
                "y": {"field": y_name, "type": "quantitative", "title": y_label},
            },
            "layer": [
                {
                    "mark": {"type": "point", "filled": True},
                    "encoding": {
                        "tooltip": [
                            {"field": "week", "type": "temporal"},
                            {"field": x_name},
                            {"field": y_name},
                        ]
                    },
                },
                *(
                    [{"transform": [{"regression": y_name, "on": x_name}], "mark": "line"}]
                    if r is not None
                    else []
                ),
            ],
        }
    )
    return DigResult(
        data,
        chart,
        f"Across {n} local ISO weeks, {x_label.lower()} and {y_label.lower()} "
        f"had {description} ({statistic}). This is a correlation, not a cause." + note,
        Headline(
            f"Pearson correlation · {n} weeks", round(r, 2) if r is not None else "Undefined", "r"
        ),
        f"Weekly scatter of {x_label.lower()} versus {y_label.lower()}; {statistic}, "
        f"n = {n}. Weeks observed in either stream are included; missing activity "
        "in the other stream is zero. Correlation, not a cause." + note,
    )


@dataclass
class CrossDig:
    id: str
    title: str
    requires: list[str] = field(default_factory=list)

    def compute(self, store: Store, params: DigParams) -> DigResult:
        if self.id == "cross.life_timeline":
            return _timeline(store, params)
        if self.id == "cross.soundtrack":
            return _soundtrack(store, params)
        if self.id in ("cross.spend_vs_listening", "cross.messages_vs_youtube"):
            return _correlation(store, params, money=self.id == "cross.spend_vs_listening")
        raise ValueError(f"unknown cross-source metric: {self.id}")


DIGS: list[Dig] = [
    CrossDig("cross.life_timeline", "Life timeline"),
    CrossDig("cross.soundtrack", "Soundtrack of a period", ["media_plays"]),
    CrossDig("cross.spend_vs_listening", "Spend versus listening", ["transactions", "media_plays"]),
    CrossDig("cross.messages_vs_youtube", "Messages versus YouTube", ["messages", "events"]),
]
