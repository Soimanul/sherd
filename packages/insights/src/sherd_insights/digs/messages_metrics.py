"""Message metrics computed in SQL, with local calendar bucketing."""

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline


def _contact_selection() -> str:
    """Use stable grouping keys and follow historical merges to the final label."""
    return """WITH RECURSIVE contact_targets AS (
        SELECT id, id AS final_id FROM contacts WHERE merged_into IS NULL
        UNION ALL
        SELECT c.id, t.final_id FROM contacts c
        JOIN contact_targets t ON c.merged_into = t.id
    ), local AS (
        SELECT m.*, coalesce(t.final_id, m.contact_id, m.chat_name, 'Unknown') AS contact_key,
            coalesce(c.display_name, m.chat_name, 'Unknown') AS contact,
            timezone(?, m.ts) AS local_ts
        FROM messages m LEFT JOIN contact_targets t ON t.id = m.contact_id
        LEFT JOIN contacts c ON c.id = coalesce(t.final_id, m.contact_id)
        WHERE m.kind IN ('text','media')
    ), selected AS (
        SELECT * FROM local WHERE (?::DATE IS NULL OR local_ts::DATE >= ?::DATE)
        AND (?::DATE IS NULL OR local_ts::DATE <= ?::DATE)
    """


def selection(params: DigParams, *, direct: bool = True) -> tuple[str, list[object]]:
    if params.top_n < 1:
        raise ValueError("top_n must be at least 1")
    if params.granularity not in ("day", "week", "month", "year"):
        raise ValueError("unknown granularity")
    sql = _contact_selection()
    if direct:
        sql += " AND chat_kind = 'direct'"
    return sql + ") ", [
        params.tz,
        params.date_from,
        params.date_from,
        params.date_to,
        params.date_to,
    ]


def empty(data: pa.Table) -> DigResult:
    return DigResult(
        data,
        None,
        "No messages to explore in this range yet.",
        None,
        "No chart: there are no matching messages.",
    )


def result(
    data: pa.Table, chart: dict[str, Any], headline: Headline, narrative: str, summary: str
) -> DigResult:
    return DigResult(data, chart, narrative, headline, summary)


@dataclass
class MessageDig:
    id: str
    title: str
    requires: list[str] = field(default_factory=lambda: ["messages"])

    def compute(self, store: Store, params: DigParams) -> DigResult:
        prefix, args = selection(params, direct=self.id != "messages.activity_heatmap")
        metric = self.id.removeprefix("messages.")
        if metric == "volume_by_contact":
            data = store.query(
                prefix
                + """
                , top_contacts AS (SELECT contact_key FROM selected GROUP BY contact_key
                    ORDER BY count(*) DESC, contact_key LIMIT ?)
                SELECT min(contact) AS contact, date_trunc(?, local_ts)::DATE AS bucket,
                    count(*) AS messages
                FROM selected WHERE contact_key IN (SELECT contact_key FROM top_contacts)
                GROUP BY contact_key, bucket ORDER BY bucket, contact
            """,
                [*args, params.top_n, params.granularity],
            )
            if not data.num_rows:
                return empty(data)
            totals = store.query(
                prefix
                + """SELECT min(contact) AS contact, count(*) AS messages FROM selected
                GROUP BY contact_key ORDER BY messages DESC, contact LIMIT 1""",
                args,
            ).to_pylist()[0]
            return result(
                data,
                charts.line(data, "bucket", "messages", series="contact"),
                Headline(str(totals["contact"]), int(totals["messages"]), "messages"),
                f"{totals['contact']} led with {totals['messages']:,} messages.",
                "Message counts by local calendar bucket and contact.",
            )
        if metric == "top_contacts_by_year":
            ranked_sql = (
                prefix
                + """
                , counts AS (SELECT contact_key, min(contact) AS contact, year(local_ts) AS year,
                    count(*) AS messages
                    FROM selected GROUP BY contact_key, year), ranked AS (
                    SELECT *, row_number() OVER (PARTITION BY year
                        ORDER BY messages DESC, contact) AS rank FROM counts)
            """
            )
            data = store.query(
                ranked_sql + "SELECT contact, year, messages, rank FROM ranked "
                "WHERE rank <= ? ORDER BY year, rank",
                [*args, min(params.top_n, 5)],
            )
            if not data.num_rows:
                return empty(data)
            improved = store.query(
                ranked_sql
                + """
                , changes AS (SELECT DISTINCT contact_key, contact,
                    first_value(rank) OVER w - last_value(rank) OVER w AS rise
                    FROM ranked WINDOW w AS (PARTITION BY contact_key ORDER BY year
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING))
                SELECT contact, rise FROM changes ORDER BY rise DESC, contact LIMIT 1
            """,
                args,
            ).to_pylist()[0]
            winner = str(improved["contact"])
            rise = int(improved["rise"])
            return result(
                data,
                charts.bump(data, "year", "rank", "contact"),
                Headline("Biggest rise", winner)
                if rise > 0
                else Headline("Top contact", str(data["contact"][0].as_py())),
                (
                    f"{winner} rose {rise} places between their first and last years with messages."
                    if rise > 0
                    else "No contact moved up between their first and last years with messages."
                ),
                "Yearly contact ranks, with rank one at the top.",
            )
        if metric == "response_times":
            reply_cte = """, ordered AS (
                SELECT *, lag(ts) OVER w AS previous_ts, lag(is_from_me) OVER w AS previous_me
                FROM selected WINDOW w AS (PARTITION BY source, chat_id ORDER BY ts, id)
            ), replies AS (SELECT *, epoch(ts - previous_ts) / 60 AS minutes FROM ordered
                WHERE is_from_me != previous_me AND ts - previous_ts <= INTERVAL '24 hours') """
            data = store.query(
                prefix
                + reply_cte
                + """
                , top_contacts AS (SELECT contact_key FROM replies GROUP BY contact_key
                    ORDER BY count(*) DESC, contact_key LIMIT ?)
                SELECT min(contact) AS contact,
                    CASE WHEN is_from_me THEN 'you' ELSE 'them' END AS side,
                    median(minutes) AS minutes, count(*) AS replies FROM replies
                WHERE contact_key IN (SELECT contact_key FROM top_contacts)
                GROUP BY contact_key, side ORDER BY contact, side
            """,
                [*args, params.top_n],
            )
            if not data.num_rows:
                return empty(data)
            median = store.query(
                prefix
                + reply_cte
                + "SELECT median(minutes) AS minutes FROM replies WHERE is_from_me",
                args,
            ).to_pylist()[0]["minutes"]
            headline = (
                Headline("Your median reply", round(median, 1), "minutes")
                if median is not None
                else Headline("Your median reply", "No replies")
            )
            narrative = (
                f"Your median reply took {median:.1f} minutes."
                if median is not None
                else "There are replies from contacts, but none from you in this range."
            )
            return result(
                data,
                charts.bar(
                    data,
                    "contact",
                    "minutes",
                    series="side",
                    paired=True,
                    series_order=["you", "them"],
                ),
                headline,
                narrative,
                "Median reply minutes for you and each contact; gaps over 24 hours are excluded.",
            )
        if metric == "conversation_starters":
            data = store.query(
                prefix
                + """
                , ordered AS (SELECT *, lag(ts) OVER (PARTITION BY source, chat_id
                    ORDER BY ts, id) AS previous_ts FROM selected), starts AS (
                    SELECT * FROM ordered WHERE previous_ts IS NULL
                        OR ts - previous_ts >= INTERVAL '6 hours'), counts AS (
                    SELECT min(contact) AS contact, count(*) AS conversations,
                        count(*) FILTER (WHERE is_from_me) AS started_by_you FROM starts
                    GROUP BY contact_key)
                SELECT *, started_by_you::DOUBLE / conversations AS your_share,
                    sum(started_by_you) OVER ()::DOUBLE /
                    sum(conversations) OVER () AS overall_share
                FROM counts ORDER BY conversations DESC, contact LIMIT ?
            """,
                [*args, params.top_n],
            )
            if not data.num_rows:
                return empty(data)
            share = float(data.to_pylist()[0]["overall_share"])
            values = [
                {"contact": r["contact"], "side": side, "share": value}
                for r in data.to_pylist()
                for side, value in (("you", r["your_share"]), ("them", 1 - r["your_share"]))
            ]
            return result(
                data,
                charts.bar(values, "contact", "share", series="side", series_order=["you", "them"]),
                Headline("Conversations you started", round(share * 100, 1), "%"),
                f"You started {share:.0%} of conversations after at least six hours of silence.",
                "Share of conversations started by you and by each contact.",
            )
        if metric == "activity_heatmap":
            data = store.query(
                prefix
                + """SELECT hour(local_ts) AS hour,
                isodow(local_ts) AS weekday, count(*) AS messages FROM selected WHERE is_from_me
                GROUP BY hour, weekday ORDER BY weekday, hour""",
                args,
            )
            if not data.num_rows:
                return empty(data)
            rows = data.to_pylist()
            share = sum(r["messages"] for r in rows if r["hour"] < 5) / sum(
                r["messages"] for r in rows
            )
            return result(
                data,
                charts.heatmap(data, "hour", "weekday", "messages"),
                Headline("Night-owl index", round(share * 100, 1), "%"),
                f"You sent {share:.0%} of your messages between midnight and 5 am local time.",
                "Your sent messages by local hour and weekday, Monday one through Sunday seven.",
            )
        if metric == "streaks_silences":
            data = store.query(
                prefix
                + """
                , days AS (SELECT DISTINCT contact_key, contact, local_ts::DATE AS day
                    FROM selected),
                numbered AS (SELECT *, day - (row_number() OVER
                    (PARTITION BY contact_key ORDER BY day))::INTEGER AS run,
                    date_diff('day', lag(day) OVER
                    (PARTITION BY contact_key ORDER BY day), day) AS gap FROM days),
                runs AS (SELECT contact_key, min(contact) AS contact, run,
                    count(*) AS streak, max(gap) AS silence
                    FROM numbered GROUP BY contact_key, run)
                SELECT min(contact) AS contact, max(streak) AS streak_days,
                    coalesce(max(silence), 0) AS silence_days
                FROM runs GROUP BY contact_key ORDER BY streak_days DESC, contact LIMIT ?
            """,
                [*args, params.top_n],
            )
            if not data.num_rows:
                return empty(data)
            first = data.to_pylist()[0]
            values = [
                {"contact": r["contact"], "metric": label, "days": r[col]}
                for r in data.to_pylist()
                for label, col in (("streak", "streak_days"), ("silence", "silence_days"))
            ]
            return result(
                data,
                charts.bar(values, "contact", "days", series="metric", paired=True),
                Headline("Longest streak", int(first["streak_days"]), "days"),
                f"Your longest streak with {first['contact']} lasted {first['streak_days']} days.",
                "Longest consecutive local days with messages and longest days "
                "between messages per contact.",
            )
        raise ValueError(f"unknown message metric: {metric}")


DIGS: list[Dig] = [
    MessageDig("messages." + metric, title)
    for metric, title in (
        ("volume_by_contact", "Message volume by contact"),
        ("top_contacts_by_year", "Top contacts by year"),
        ("response_times", "Reply times"),
        ("conversation_starters", "Conversation starters"),
        ("activity_heatmap", "Activity by local hour"),
        ("streaks_silences", "Streaks and silences"),
    )
]
