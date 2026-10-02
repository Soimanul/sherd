"""Own text vocabulary and emoji, without sending message text anywhere."""

import re
from collections import Counter
from dataclasses import dataclass, field

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline
from sherd_insights.digs.messages_metrics import empty, result, selection

WORDS = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
URLS = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
STOPWORD_TEXT = (
    "the and you your that this with for from are was were have has had not but can "
    "este sunt care pentru acest aceasta sau din cum mai fost lui lor noi voi "
    "der die das und ist sind mit von den dem ein eine nicht ich du wir sie "
    "los las del que por para con una uno como pero sus son esta este "
    "les des une un que qui dans pour avec est sont pas nous vous elle ses"
)
STOPWORDS = frozenset(STOPWORD_TEXT.split())
# Emoji bases, flags and keycaps; modifiers and joiners are consumed as part of a cluster.
BASE = (
    r"(?:[\U0001F1E6-\U0001F1FF]{2}|[0-9#*]\ufe0f?\u20e3|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF])"
)
PART = BASE + r"[\ufe0f\ufe0e]?[\U0001F3FB-\U0001F3FF]?[\U000E0020-\U000E007E]*\U000E007F?"
EMOJI = re.compile(PART + r"(?:\u200d" + PART + r")*")


@dataclass
class EmojiWords:
    id: str = "messages.emoji_words"
    title: str = "Your emoji and words by year"
    requires: list[str] = field(default_factory=lambda: ["messages"])

    def compute(self, store: Store, params: DigParams) -> DigResult:
        prefix, args = selection(params, direct=False)
        source = store.query(
            prefix
            + """SELECT year(local_ts) AS year, text FROM selected
            WHERE is_from_me AND kind = 'text' AND text IS NOT NULL ORDER BY ts, id""",
            args,
        )
        counters: dict[tuple[int, str], Counter[str]] = {}
        total: Counter[str] = Counter()
        for batch in source.to_batches(max_chunksize=1024):
            for row in batch.to_pylist():
                text = URLS.sub("", row["text"])
                emoji = EMOJI.findall(text)
                total.update(emoji)
                counters.setdefault((row["year"], "emoji"), Counter()).update(emoji)
                counters.setdefault((row["year"], "word"), Counter()).update(
                    word for word in WORDS.findall(text.lower()) if word not in STOPWORDS
                )
        rows = [
            {"year": year, "kind": kind, "token": token, "count": count}
            for (year, kind), counts in sorted(counters.items())
            for token, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
                : params.top_n
            ]
        ]
        columns: dict[str, pa.DataType] = {
            "year": pa.int64(),
            "kind": pa.string(),
            "token": pa.string(),
            "count": pa.int64(),
        }
        data = pa.Table.from_pylist(rows, schema=pa.schema(columns))
        if not rows:
            return empty(data)
        emoji_rows = [r for r in rows if r["kind"] == "emoji"]
        chart = charts.bar(emoji_rows, "token", "count")
        chart["encoding"]["column"] = {"field": "year", "type": "ordinal"}
        if total:
            token, count = sorted(total.items(), key=lambda item: (-item[1], item[0]))[0]
            headline = Headline("Your most-used emoji", token)
            narrative = f"You used {token} {count:,} times in your text messages."
        else:
            headline = Headline("Your most-used emoji", "No emoji")
            narrative = "Your text messages contain words but no emoji in this range."
        return result(
            data,
            chart,
            headline,
            narrative,
            "Most-used emoji per local year; the table also lists words "
            "with common stopwords removed.",
        )


DIGS: list[Dig] = [EmojiWords()]
