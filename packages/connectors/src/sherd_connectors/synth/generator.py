"""Deterministic synthetic canonical rows (PLAN §5).

One fictional person ("Alex Demo", living in Bucharest) over 2023-10-01 to 2026-09-30. Rows come
in source blocks — WhatsApp messages, Spotify plays, bank transactions, Google Takeout events,
shell commands — each in day order, so a consumer can split them by `source_of` while streaming.

Scale: `demo` ≈ 50k rows; `bench` multiplies every rate-driven stream (conversations, listening
sessions, card payments, transfers, browsing, shell sessions) by `PROFILES["bench"]` = 40, while
calendar items (salary, rent, subscriptions, exchanges, fees) stay monthly: ≈ 2M rows.
"""

import base64
import hashlib
import math
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import chain
from typing import Literal
from zoneinfo import ZoneInfo

from sherd_core.ids import row_id
from sherd_core.models import Event, MediaPlay, Message, Row, Transaction

from sherd_connectors.synth import data

Profile = Literal["demo", "bench"]
PROFILES: dict[str, int] = {"demo": 1, "bench": 40}

TZ = ZoneInfo("Europe/Bucharest")
START = date(2023, 10, 1)
END = date(2026, 9, 30)
START_UTC = datetime.combine(START, time(), TZ).astimezone(UTC)
END_UTC = datetime.combine(END + timedelta(days=1), time(), TZ).astimezone(UTC)

SELF_IDENTITIES = frozenset({data.SELF_NAME, data.SELF_PHONE})

# Relative activity by hour of local day.
_DAY_HOURS = (
    0.3, 0.15, 0.05, 0.02, 0.02, 0.05, 0.3, 1.0, 1.6, 1.5, 1.4, 1.5,
    2.0, 1.8, 1.4, 1.4, 1.5, 1.8, 2.2, 2.6, 2.8, 2.6, 1.8, 0.9,
)  # fmt: skip
_OWL_HOURS = (
    2.6, 2.4, 1.8, 1.0, 0.3, 0.05, 0.02, 0.02, 0.05, 0.1, 0.2, 0.4,
    0.6, 0.6, 0.5, 0.5, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.6, 3.0,
)  # fmt: skip
_WORK_HOURS = (
    0, 0, 0, 0, 0, 0, 0, 0.2, 1.0, 2.0, 2.2, 2.0,
    1.0, 1.6, 2.2, 2.0, 1.8, 1.2, 0.5, 0.3, 0.4, 0.5, 0.3, 0.1,
)  # fmt: skip
_SHOP_HOURS = (
    0, 0, 0, 0, 0, 0, 0, 0.3, 1.0, 0.8, 0.8, 1.0,
    1.6, 1.4, 0.9, 0.9, 1.1, 1.6, 2.0, 2.0, 1.6, 1.0, 0.4, 0.1,
)  # fmt: skip
_EVENING_HOURS = (
    0.6, 0.3, 0.1, 0, 0, 0, 0, 0.1, 0.2, 0.2, 0.3, 0.4,
    0.6, 0.5, 0.4, 0.4, 0.5, 0.8, 1.2, 1.8, 2.4, 2.6, 2.2, 1.4,
)  # fmt: skip
_MESSAGE_WEEKDAY = (0.85, 0.9, 0.95, 0.95, 1.15, 1.45, 1.35)
_TIER_RATE = {"close": 0.34, "regular": 0.1, "light": 0.03}


def source_of(row: Row) -> str:
    """The connector a real export of this row would come from."""
    if isinstance(row, Message):
        return "whatsapp"
    if isinstance(row, MediaPlay):
        return "spotify"
    if isinstance(row, Transaction):
        return "bank_csv"
    if isinstance(row, Event):
        return "shell_history" if row.kind == "shell.command" else "google_takeout"
    raise TypeError(f"no synthetic source for {type(row).__name__}")


def generate(profile: Profile, seed: int = 42) -> Iterator[Row]:
    """Stream the profile's rows: same profile and seed, same rows, in the same order."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(PROFILES)}")
    return chain(
        _messages(_Stream(profile, seed, "msg")),
        _plays(_Stream(profile, seed, "play")),
        _transactions(_Stream(profile, seed, "txn")),
        _browsing(_Stream(profile, seed, "web")),
        _shell(_Stream(profile, seed, "sh")),
    )


def fingerprint(rows: Iterator[Row]) -> str:
    """sha256 over the rows' table names and JSON dumps, for determinism checks."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.table_name.encode())
        digest.update(row.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


# -- helpers -----------------------------------------------------------------------------------


class _Stream:
    """One independently seeded stream of rows with its own id counter."""

    def __init__(self, profile: str, seed: int, name: str) -> None:
        self.rng = random.Random(f"{seed}:{name}")
        self.scale = PROFILES[profile]
        self.source_file = f"synthetic/{profile}"
        self._prefix = f"synth:{name}:"
        self._count = 0

    def next_id(self) -> str:
        self._count += 1
        return f"{self._prefix}{self._count:08d}"

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, round(self.rng.gauss(lam, math.sqrt(lam))))
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def hour_offset(self, weights: Sequence[float]) -> float:
        """Seconds after local midnight, drawn from an hourly weight profile."""
        hour = self.rng.choices(range(24), weights)[0]
        return hour * 3600 + self.rng.random() * 3600


def _days() -> Iterator[date]:
    day = START
    while day <= END:
        yield day
        day += timedelta(days=1)


def _at(day: date, seconds: float) -> datetime:
    """The UTC instant `seconds` of local wall-clock time after `day`'s local midnight."""
    local = datetime.combine(day, time(), TZ) + timedelta(seconds=seconds)
    return local.astimezone(UTC)


def _windows(
    rng: random.Random, active: tuple[date, date], count: int, days: tuple[int, int]
) -> list[tuple[date, date]]:
    span = (active[1] - active[0]).days
    found = []
    for _ in range(count):
        start = active[0] + timedelta(days=rng.randrange(max(span, 1)))
        found.append((start, start + timedelta(days=rng.randint(*days))))
    return found


def _within(day: date, windows: Sequence[tuple[date, date]]) -> bool:
    return any(start <= day <= end for start, end in windows)


def _token(text: str, length: int) -> str:
    """A stable url-safe id derived from `text` that starts with a digit."""
    raw = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).decode()
    return str(len(text) % 10) + raw.replace("-", "x").replace("_", "y")[: length - 1]


# -- messages ----------------------------------------------------------------------------------


@dataclass
class _Chat:
    chat_id: str
    name: str
    kind: Literal["direct", "group"]
    members: tuple[data.Contact, ...]
    active: tuple[date, date]
    rate: float
    hours: Sequence[float]
    my_share: float
    weekdays_only: bool
    silences: list[tuple[date, date]]
    streaks: list[tuple[date, date]]
    created: bool = False


def _chats(stream: _Stream) -> list[_Chat]:
    rng = stream.rng
    by_name = {contact.name: contact for contact in data.CONTACTS}
    chats = []
    for contact in data.CONTACTS:
        chats.append(
            _Chat(
                chat_id=f"whatsapp:{contact.phone}",
                name=contact.name,
                kind="direct",
                members=(contact,),
                active=contact.active,
                rate=_TIER_RATE[contact.tier],
                hours=_OWL_HOURS if contact.night_owl else _DAY_HOURS,
                my_share=contact.my_share,
                weekdays_only=False,
                silences=_windows(rng, contact.active, rng.randint(1, 3), (10, 40)),
                streaks=(
                    _windows(rng, contact.active, rng.randint(2, 4), (14, 28))
                    if contact.tier == "close"
                    else []
                ),
            )
        )
    for group in data.GROUPS:
        chats.append(
            _Chat(
                chat_id=f"whatsapp:group:{group.slug}",
                name=group.name,
                kind="group",
                members=tuple(by_name[name] for name in group.members),
                active=group.active,
                rate=group.rate * 0.4,
                hours=_WORK_HOURS if group.weekdays_only else _DAY_HOURS,
                my_share=0.4,
                weekdays_only=group.weekdays_only,
                silences=_windows(rng, group.active, 1, (7, 21)),
                streaks=[],
            )
        )
    return chats


def _messages(stream: _Stream) -> Iterator[Message]:
    chats = _chats(stream)
    for day in _days():
        weekday = day.weekday()
        holiday = (
            1.25 if (day.month, day.day) >= (12, 20) or (day.month, day.day) <= (1, 2) else 1.0
        )
        for chat in chats:
            if not chat.active[0] <= day <= chat.active[1]:
                continue
            if chat.weekdays_only and weekday >= 5:
                continue
            if chat.kind == "group" and not chat.created:
                chat.created = True
                yield _group_created(stream, chat, day)
            if _within(day, chat.silences):
                continue
            rate = chat.rate * _MESSAGE_WEEKDAY[weekday] * holiday
            if _within(day, chat.streaks):
                rate *= 2.5
            for _ in range(stream.poisson(rate * stream.scale)):
                yield from _conversation(stream, chat, day)


def _group_created(stream: _Stream, chat: _Chat, day: date) -> Message:
    return Message(
        source_file=stream.source_file,
        source_row_id=stream.next_id(),
        chat_id=chat.chat_id,
        chat_name=chat.name,
        chat_kind="group",
        is_from_me=False,
        ts=_at(day, 9 * 3600 + stream.rng.random() * 3600),
        text=f'{chat.members[0].name} created group "{chat.name}"',
        kind="system",
    )


def _conversation(stream: _Stream, chat: _Chat, day: date) -> Iterator[Message]:
    rng = stream.rng
    clock = stream.hour_offset(chat.hours)
    group = chat.kind == "group"
    turns = rng.randint(3, 14) if group else rng.randint(2, 10)
    previous: tuple[str, str] | None = None  # (speaker, source_row_id)
    for turn in range(turns):
        mine = rng.random() < chat.my_share
        contact = rng.choice(chat.members)
        speaker = data.SELF_NAME if mine else contact.name
        if previous is not None:
            if previous[0] == speaker:
                clock += rng.uniform(4, 90)
            else:
                minutes = (
                    3.0 if group else (contact.my_reply_minutes if mine else contact.reply_minutes)
                )
                clock += min(rng.lognormvariate(math.log(minutes * 60), 0.8), 8 * 3600)
        ts = _at(day, clock)
        if ts >= END_UTC:
            return
        source_row_id = stream.next_id()
        reply_to = None
        if group and previous is not None and previous[0] != speaker and rng.random() < 0.12:
            reply_to = row_id("whatsapp", previous[1])
        if group and turn == 0 and rng.random() < 0.04:
            yield Message(
                source_file=stream.source_file,
                source_row_id=source_row_id,
                chat_id=chat.chat_id,
                chat_name=chat.name,
                chat_kind="group",
                is_from_me=False,
                ts=ts,
                text=rng.choice(data.GROUP_SYSTEM).format(a=contact.name, b=chat.members[-1].name),
                kind="system",
            )
            continue
        kind, text, media_type = _content(rng, group)
        yield Message(
            source_file=stream.source_file,
            source_row_id=source_row_id,
            chat_id=chat.chat_id,
            chat_name=chat.name,
            chat_kind=chat.kind,
            sender_id=f"whatsapp:{data.SELF_PHONE if mine else contact.phone}",
            sender_name=speaker,
            is_from_me=mine,
            ts=ts,
            text=text,
            kind=kind,
            media_type=media_type,
            reply_to_id=reply_to,
        )
        previous = (speaker, source_row_id)


_MessageKind = Literal["text", "media", "deleted"]


def _content(
    rng: random.Random, group: bool
) -> tuple[_MessageKind, str | None, data.MediaType | None]:
    roll = rng.random()
    if roll < 0.012:
        return "deleted", None, None
    if roll < 0.075:
        names = [name for name, _ in data.MEDIA_TYPES]
        media_type = rng.choices(names, [weight for _, weight in data.MEDIA_TYPES])[0]
        caption = (
            rng.choice(data.CAPTIONS)
            if media_type in ("image", "video") and rng.random() < 0.3
            else None
        )
        return "media", caption, media_type
    phrases = data.GROUP_PHRASES if group and rng.random() < 0.4 else data.PHRASES
    return "text", rng.choice(phrases), None


# -- music -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Track:
    artist: str
    title: str
    album: str
    duration_ms: int
    uri: str


def _catalogue() -> dict[str, list[_Track]]:
    """Artist → tracks, most played first. Fixed: independent of the profile seed."""
    rng = random.Random("sherd-synth-catalogue")
    words = data.TITLE_WORDS
    catalogue: dict[str, list[_Track]] = {}
    for artist, _ in data.ARTISTS:
        albums = [f"{rng.choice(words)} {rng.choice(words)}" for _ in range(rng.randint(1, 2))]
        titles: list[str] = []
        if artist == data.OBSESSION_ARTIST:
            titles.append(data.OBSESSION_TRACK)
        while len(titles) < rng.randint(6, 11):
            title = f"{rng.choice(words)} {rng.choice(words)}"
            if title not in titles and title != data.OBSESSION_TRACK:
                titles.append(title)
        catalogue[artist] = [
            _Track(
                artist,
                title,
                rng.choice(albums),
                rng.randint(140_000, 330_000),
                f"spotify:track:{_token(artist + '/' + title, 22)}",
            )
            for title in titles
        ]
    return catalogue


def _season(day: date) -> str:
    if day.month in (6, 7, 8):
        return "summer"
    if day.month in (12, 1, 2):
        return "winter"
    return "none"


def _artist_weights(slot: str, season: str) -> list[float]:
    weights = []
    for _, mood in data.ARTISTS:
        if mood in ("summer", "winter"):
            weight = 3.0 if mood == season else 0.3
        elif mood == "morning":
            weight = 3.0 if slot == "morning" else 0.6
        elif mood == "night":
            weight = 3.0 if slot == "night" else 0.6
        else:
            weight = 1.0
        weights.append(weight)
    return weights


# (slot, local start hour range, sessions per day weekday/weekend, plays per session)
_LISTENING: tuple[tuple[str, tuple[float, float], tuple[float, float], tuple[int, int]], ...] = (
    ("morning", (7.3, 9.0), (0.8, 0.0), (4, 8)),
    ("late_morning", (10.0, 12.5), (0.0, 0.6), (4, 9)),
    ("afternoon", (14.0, 18.0), (0.1, 0.7), (5, 12)),
    ("evening", (18.5, 22.5), (0.7, 0.4), (6, 12)),
    ("night", (23.0, 25.0), (0.25, 0.35), (3, 7)),
)


def _plays(stream: _Stream) -> Iterator[MediaPlay]:
    rng = stream.rng
    catalogue = _catalogue()
    artists = [artist for artist, _ in data.ARTISTS]
    obsession = catalogue[data.OBSESSION_ARTIST][0]
    weight_cache: dict[tuple[str, str], list[float]] = {}
    episodes = 0
    for day in _days():
        weekend = day.weekday() >= 5
        obsessed = data.OBSESSION_WEEK[0] <= day <= data.OBSESSION_WEEK[1]
        season = _season(day)
        for slot, hours, rates, size in _LISTENING:
            rate = rates[1] if weekend else rates[0]
            if obsessed and slot == "evening":
                rate += 1.0
            for _ in range(stream.poisson(rate * stream.scale)):
                clock = rng.uniform(*hours) * 3600
                shuffle = rng.random() < 0.4
                platform = "ios" if slot == "morning" or rng.random() < 0.35 else "macos"
                if rng.random() < 0.03:
                    platform = "web"
                if slot == "morning" and rng.random() < 0.3:
                    episodes += 1
                    show = rng.choice(data.PODCASTS)
                    ms = rng.randint(15, 55) * 60_000
                    clock += ms / 1000
                    ts = _at(day, clock)
                    if ts < END_UTC:
                        yield MediaPlay(
                            source_file=stream.source_file,
                            source_row_id=stream.next_id(),
                            ts=ts,
                            media_kind="episode",
                            artist=show,
                            track=f"Episode {episodes}: {rng.choice(data.TITLE_WORDS)}",
                            uri=f"spotify:episode:{_token(f'{show}/{episodes}', 22)}",
                            ms_played=ms,
                            platform=platform,
                            shuffle=False,
                            skipped=False,
                        )
                    continue
                weights = weight_cache.get((slot, season))
                if weights is None:
                    weights = weight_cache[(slot, season)] = _artist_weights(slot, season)
                for _ in range(rng.randint(*size)):
                    if obsessed and rng.random() < 0.45:
                        track = obsession
                    else:
                        tracks = catalogue[rng.choices(artists, weights)[0]]
                        track = rng.choices(tracks, [1 / (i + 1) for i in range(len(tracks))])[0]
                    skipped = rng.random() < (0.3 if slot == "morning" else 0.2)
                    if track is obsession and obsessed:
                        skipped = False
                    ms = rng.randint(1_500, 30_000) if skipped else track.duration_ms
                    clock += ms / 1000 + rng.uniform(0, 2)
                    ts = _at(day, clock)
                    if ts >= END_UTC:
                        break
                    yield MediaPlay(
                        source_file=stream.source_file,
                        source_row_id=stream.next_id(),
                        ts=ts,
                        media_kind="track",
                        artist=track.artist,
                        track=track.title,
                        album=track.album,
                        uri=track.uri,
                        ms_played=ms,
                        platform=platform,
                        shuffle=shuffle,
                        skipped=skipped,
                    )


# -- money -------------------------------------------------------------------------------------

_ACCOUNTS = {"RON": "Revolut RON", "EUR": "Revolut EUR"}
_CENT = Decimal("0.01")


@dataclass
class _Draft:
    clock: float
    amount: Decimal
    currency: str
    kind: Literal["card", "transfer", "fee", "topup", "exchange", "refund", "other"]
    category: str
    merchant: str | None = None
    merchant_raw: str | None = None
    counterparty: str | None = None


def _money(rng: random.Random, low: float, high: float) -> Decimal:
    return Decimal(str(round(rng.uniform(low, high), 2))).quantize(_CENT)


def _payday(day: date) -> bool:
    """Salary lands on the 10th, or on the Friday before when the 10th is a weekend."""
    tenth = day.replace(day=10)
    return day == tenth - timedelta(days=max(0, tenth.weekday() - 4))


def _salary(day: date) -> tuple[str, Decimal]:
    employer, base = data.EMPLOYERS[0][1], data.EMPLOYERS[0][2]
    for since, name, amount in data.EMPLOYERS:
        if day >= since:
            employer, base = name, amount
    raise_years = (day - START).days // 365
    return employer, (Decimal(base) * Decimal("1.05") ** raise_years).quantize(_CENT)


def _transactions(stream: _Stream) -> Iterator[Transaction]:
    rng = stream.rng
    balances = {"RON": Decimal("8450.00"), "EUR": Decimal("620.00")}
    merchants = data.CARD_MERCHANTS
    for day in _days():
        weekend = day.weekday() >= 4  # Friday to Sunday
        winter = day.month in (11, 12, 1, 2)
        travelling = next((t for t in data.TRAVEL if t[0] <= day <= t[1]), None)
        drafts: list[_Draft] = []
        if _payday(day):
            employer, amount = _salary(day)
            drafts.append(
                _Draft(9 * 3600 + 600, amount, "RON", "transfer", "salary", counterparty=employer)
            )
        if day.day == 5:
            drafts.append(
                _Draft(
                    8 * 3600,
                    Decimal("-2200.00"),
                    "RON",
                    "transfer",
                    "rent",
                    counterparty="Hearth Rentals SRL",
                )
            )
        if day.day == 25:
            ron = Decimal("-1000.00")
            eur = (Decimal("1000") / Decimal(str(round(rng.uniform(4.95, 5.0), 4)))).quantize(_CENT)
            drafts.append(
                _Draft(12 * 3600, ron, "RON", "exchange", "exchange", merchant="Exchange to EUR")
            )
            drafts.append(
                _Draft(
                    12 * 3600 + 1, eur, "EUR", "exchange", "exchange", merchant="Exchange from RON"
                )
            )
        if day.day == 28:
            drafts.append(
                _Draft(
                    6 * 3600, Decimal("-39.99"), "RON", "fee", "fees", merchant="Revolut Premium"
                )
            )
        for sub in data.SUBSCRIPTIONS:
            if day.day == sub.day and sub.active[0] <= day <= sub.active[1]:
                drafts.append(
                    _Draft(
                        3 * 3600 + rng.uniform(0, 3600),
                        -Decimal(sub.amount),
                        sub.currency,
                        "card",
                        "subscriptions",
                        merchant=sub.merchant,
                        merchant_raw=sub.merchant_raw,
                    )
                )
        if travelling is not None:
            for _ in range(stream.poisson(4.0 * stream.scale)):
                name, category, low, high = rng.choice(data.TRAVEL_MERCHANTS)
                merchant = f"{name} {travelling[2]}"
                drafts.append(
                    _Draft(
                        stream.hour_offset(_SHOP_HOURS),
                        -_money(rng, low, high),
                        "EUR",
                        "card",
                        category,
                        merchant=merchant,
                        merchant_raw=merchant.upper(),
                    )
                )
        else:
            weights = []
            for name, _, category, _, _, weight in merchants:
                if weekend and name in data.WEEKEND_MERCHANTS:
                    weight *= 2.5
                if winter and category == "food_delivery":
                    weight *= 1.5
                weights.append(weight)
            for _ in range(stream.poisson((2.6 if weekend else 1.5) * stream.scale)):
                name, raw, category, low, high, _ = rng.choices(merchants, weights)[0]
                amount = _money(rng, low, high) * (Decimal("1.15") if weekend else 1)
                drafts.append(
                    _Draft(
                        stream.hour_offset(_SHOP_HOURS),
                        -amount.quantize(_CENT),
                        "RON",
                        "card",
                        category,
                        merchant=name,
                        merchant_raw=raw,
                    )
                )
                if rng.random() < 0.01:
                    drafts.append(
                        _Draft(
                            stream.hour_offset(_SHOP_HOURS),
                            amount.quantize(_CENT),
                            "RON",
                            "refund",
                            category,
                            merchant=name,
                            merchant_raw=raw,
                        )
                    )
        friends = [
            c for c in data.CONTACTS if c.active[0] <= day <= c.active[1] and c.name != "Mama"
        ]
        for _ in range(stream.poisson(0.25 * stream.scale)):
            friend = rng.choice(friends)
            incoming = rng.random() < 0.3
            amount = _money(rng, 20, 250)
            drafts.append(
                _Draft(
                    stream.hour_offset(_EVENING_HOURS),
                    amount if incoming else -amount,
                    "RON",
                    "transfer",
                    "transfers",
                    counterparty=friend.name,
                )
            )
        drafts.sort(key=lambda d: d.clock)
        for draft in drafts:
            ts = _at(day, draft.clock)
            if ts >= END_UTC:
                continue
            balances[draft.currency] += draft.amount
            yield Transaction(
                source_file=stream.source_file,
                source_row_id=stream.next_id(),
                ts=ts,
                amount=draft.amount,
                currency=draft.currency,
                merchant_raw=draft.merchant_raw,
                merchant=draft.merchant,
                counterparty=draft.counterparty,
                category=draft.category,
                account=_ACCOUNTS[draft.currency],
                balance=balances[draft.currency],
                kind=draft.kind,
            )


# -- browsing and shell ------------------------------------------------------------------------


def _browsing(stream: _Stream) -> Iterator[Event]:
    rng = stream.rng
    for day in _days():
        weekend = day.weekday() >= 5
        drafts: list[tuple[float, str, str, str]] = []
        for _ in range(stream.poisson((3.5 if weekend else 1.6) * stream.scale)):
            title = f"{rng.choice(data.VIDEO_TOPICS)} #{rng.randint(1, 12)}"
            url = f"https://www.youtube.com/watch?v={_token(title, 11)}"
            hours = _DAY_HOURS if weekend else _EVENING_HOURS
            drafts.append((stream.hour_offset(hours), "youtube.watch", title, url))
        for _ in range(stream.poisson((1.2 if weekend else 2.0) * stream.scale)):
            query = rng.choice(data.SEARCHES)
            url = "https://www.google.com/search?q=" + query.replace(" ", "+")
            hours = _DAY_HOURS if weekend else _WORK_HOURS
            drafts.append((stream.hour_offset(hours), "google.search", query, url))
        for _ in range(stream.poisson(2.2 * stream.scale)):
            url, title = rng.choice(data.SITES)
            drafts.append((stream.hour_offset(_DAY_HOURS), "chrome.visit", title, url))
        drafts.sort(key=lambda d: d[0])
        for clock, kind, title, url in drafts:
            ts = _at(day, clock)
            if ts < END_UTC:
                yield Event(
                    source_file=stream.source_file,
                    source_row_id=stream.next_id(),
                    ts=ts,
                    kind=kind,
                    title=title,
                    url=url,
                )


def _shell(stream: _Stream) -> Iterator[Event]:
    rng = stream.rng
    for day in _days():
        weekend = day.weekday() >= 5
        for _ in range(stream.poisson((0.15 if weekend else 0.6) * stream.scale)):
            clock = stream.hour_offset(_EVENING_HOURS if weekend else _WORK_HOURS)
            for _ in range(rng.randint(2, 9)):
                clock += rng.uniform(3, 240)
                ts = _at(day, clock)
                if ts >= END_UTC:
                    break
                yield Event(
                    source_file=stream.source_file,
                    source_row_id=stream.next_id(),
                    ts=ts,
                    kind="shell.command",
                    title=rng.choice(data.SHELL_COMMANDS),
                )
