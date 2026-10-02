"""Fixed, clearly fictional catalogue the synthetic generator draws from.

Phone numbers are in the reserved `+1 555 0100` to `0199` range; nothing here is a real person.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

SELF_NAME = "Alex Demo"
SELF_PHONE = "+15550100"


@dataclass(frozen=True)
class Contact:
    name: str
    phone: str
    tier: str  # 'close' | 'regular' | 'light': how often we talk
    active: tuple[date, date]  # when this person is part of the circle
    my_share: float  # share of a conversation's turns that are mine
    reply_minutes: float  # their typical response delay
    my_reply_minutes: float  # mine to them
    night_owl: bool = False


_START = date(2023, 10, 1)
_END = date(2026, 9, 30)

CONTACTS: tuple[Contact, ...] = (
    Contact("Mama", "+15550101", "close", (_START, _END), 0.45, 25, 40),
    Contact("Mira Quillfeather", "+15550102", "close", (_START, _END), 0.5, 4, 6),
    Contact("Tobias Fernwood", "+15550103", "close", (_START, date(2025, 2, 28)), 0.55, 12, 9),
    Contact("Lavinia Owlcroft", "+15550104", "close", (_START, _END), 0.48, 6, 14, night_owl=True),
    Contact("Ilinca Brightwater", "+15550105", "close", (date(2024, 9, 1), _END), 0.52, 5, 5),
    Contact("Radu Pebblestone", "+15550106", "regular", (_START, _END), 0.5, 30, 20),
    Contact("Sorina Moonvale", "+15550107", "regular", (_START, date(2024, 12, 31)), 0.6, 45, 15),
    Contact("Dragos Thistledown", "+15550108", "regular", (date(2024, 9, 1), _END), 0.45, 8, 12),
    Contact("Ana Wrenfield", "+15550109", "regular", (_START, _END), 0.5, 18, 25),
    Contact("Victor Emberly", "+15550110", "regular", (date(2025, 2, 1), _END), 0.4, 3, 10),
    Contact("Elena Larkspur", "+15550111", "regular", (date(2025, 6, 1), _END), 0.55, 9, 7),
    Contact("Matei Cobblewick", "+15550112", "regular", (_START, date(2025, 8, 31)), 0.5, 60, 30),
    Contact("Irina Saltmarsh", "+15550113", "light", (_START, _END), 0.5, 120, 60),
    Contact("Andrei Hollowbrook", "+15550114", "light", (_START, date(2024, 6, 30)), 0.6, 90, 45),
    Contact("Bianca Nettlefold", "+15550115", "light", (date(2024, 3, 1), _END), 0.5, 40, 50),
    Contact("Cosmin Driftwood", "+15550116", "light", (_START, _END), 0.45, 200, 90),
    Contact("Daria Willowmere", "+15550117", "light", (date(2025, 1, 1), _END), 0.5, 35, 20),
    Contact("Petru Gorseheath", "+15550118", "light", (_START, date(2025, 3, 31)), 0.55, 75, 40),
    Contact("Sebastian Mossgrove", "+15550119", "light", (date(2024, 9, 1), _END), 0.5, 15, 25),
    Contact("Teodora Heathmoor", "+15550120", "light", (_START, _END), 0.5, 50, 35),
    Contact("Horia Bramblecote", "+15550121", "light", (date(2025, 2, 1), _END), 0.45, 20, 30),
    Contact("Nora Kettleby", "+15550122", "light", (_START, date(2024, 10, 31)), 0.5, 80, 60),
    Contact("Stefan Ashdown", "+15550123", "light", (date(2024, 5, 1), _END), 0.55, 25, 15),
    Contact("Oana Rookwood", "+15550124", "light", (_START, _END), 0.5, 150, 120),
    Contact("Lucian Fogbridge", "+15550125", "light", (date(2025, 9, 1), _END), 0.4, 10, 20),
)


@dataclass(frozen=True)
class Group:
    slug: str
    name: str
    members: tuple[str, ...]  # contact names
    active: tuple[date, date]
    rate: float  # conversations per day
    weekdays_only: bool = False


GROUPS: tuple[Group, ...] = (
    Group(
        "familia", "Familia ❤️", ("Mama", "Irina Saltmarsh", "Cosmin Driftwood"), (_START, _END), 0.5
    ),
    Group(
        "flat-4b",
        "Flat 4B 🏠",
        ("Tobias Fernwood", "Sorina Moonvale", "Matei Cobblewick"),
        (_START, date(2025, 2, 28)),
        0.6,
    ),
    Group(
        "climbing-crew",
        "Climbing Crew 🧗",
        ("Ilinca Brightwater", "Dragos Thistledown", "Sebastian Mossgrove", "Stefan Ashdown"),
        (date(2024, 9, 1), _END),
        0.8,
    ),
    Group(
        "northwind-team",
        "Northwind Team",
        ("Victor Emberly", "Horia Bramblecote", "Daria Willowmere", "Elena Larkspur"),
        (date(2025, 2, 1), _END),
        1.2,
        weekdays_only=True,
    ),
)

PHRASES: tuple[str, ...] = (
    "on my way 🚶",
    "haha true 😂",
    "coffee tomorrow? ☕",
    "running 10 min late, sorry!",
    "did you see that?",
    "send me the pics 📸",
    "sounds good 👍",
    "ok",
    "😂😂😂",
    "good morning ☀️",
    "good night 🌙",
    "where are you?",
    "almost there",
    "can't today, maybe weekend?",
    "happy birthday!! 🎉🎂",
    "thank you ❤️",
    "omg yes",
    "let me check and get back to you",
    "dinner at 8?",
    "I'm at the gym 💪",
    "the train is late again 🙄",
    "what are you up to?",
    "just finished work",
    "this song is stuck in my head 🎶",
    "did you call?",
    "call you later",
    "no worries",
    "see you there!",
    "I miss you 🥺",
    "lol",
    "true",
    "wait what",
    "how was it?",
    "so tired today 😴",
    "who's bringing snacks? 🍿",
    "rain again ☔",
    "need anything from the shop?",
    "look at this 👀",
    "yesss 🙌",
    "brb",
    "booked the tickets ✈️",
    "the cat says hi 🐈",
    "pizza tonight? 🍕",
    "can you send the address?",
    "how's your week going?",
    "sending love 💛",
    "I'll bring the board games 🎲",
    "same here",
    "finally weekend 🥳",
    "ugh mondays",
)

GROUP_PHRASES: tuple[str, ...] = (
    "who's in for saturday?",
    "I can drive 🚗",
    "count me in!",
    "same time as last week?",
    "bouldering at 7? 🧗",
    "rent is due on friday 🏠",
    "someone took my yoghurt again 😤",
    "call grandma on sunday ❤️",
    "standup moved to 10:30",
    "PR is up, please review 🙏",
    "deploy done ✅",
    "lunch? 🥗",
    "lost my chalk bag again",
    "pics from today 📸",
)

GROUP_SYSTEM: tuple[str, ...] = (
    "Messages and calls are end-to-end encrypted.",
    "{a} changed the group description",
    "{a} changed this group's icon",
    "{a} added {b}",
)

MediaType = Literal["image", "video", "audio", "document", "sticker", "gif", "other"]

MEDIA_TYPES: tuple[tuple[MediaType, float], ...] = (
    ("image", 0.5),
    ("video", 0.12),
    ("audio", 0.14),
    ("sticker", 0.12),
    ("gif", 0.06),
    ("document", 0.04),
    ("other", 0.02),
)

CAPTIONS: tuple[str, ...] = ("look 👀", "us 😄", "the view!", "new haircut", "receipt")

# -- music -------------------------------------------------------------------------------------

# (artist, mood) — mood decides when an artist gets played.
ARTISTS: tuple[tuple[str, str], ...] = (
    ("Lumen Drift", "general"),
    ("The Velvet Ostriches", "morning"),
    ("Kasimir & the Owls", "general"),
    ("Paper Comet", "morning"),
    ("Glass Orchard", "night"),
    ("Saltwater Radio", "summer"),
    ("Neon Heron", "morning"),
    ("Midnight Atlas", "night"),
    ("The Quiet Tramlines", "night"),
    ("Sunday Static", "general"),
    ("Copper Lanterns", "winter"),
    ("Juniper Satellites", "summer"),
    ("Hollow Bloom", "night"),
    ("Brass Meridian", "morning"),
    ("Snowfield Choir", "winter"),
    ("The Lemon Engines", "summer"),
    ("Velvet Static", "general"),
    ("Ferris & the Fog", "night"),
    ("Pine Arcade", "winter"),
    ("Coral Telephone", "summer"),
    ("Tin Moon Society", "general"),
    ("Polar Postcards", "winter"),
    ("The Amber Dials", "morning"),
    ("Cloudharbour", "night"),
    ("Orbit Lane", "general"),
    ("Ivory Kites", "morning"),
    ("Dune Cassette", "summer"),
    ("Woolen Thunder", "winter"),
    ("Echo Parlour", "general"),
    ("Slow Lighthouse", "night"),
)

TITLE_WORDS: tuple[str, ...] = (
    "Paper",
    "Lanterns",
    "Static",
    "Morning",
    "Harbour",
    "Silver",
    "Ghost",
    "Tides",
    "Velvet",
    "Atlas",
    "Northern",
    "Lights",
    "Summer",
    "Rain",
    "Midnight",
    "Garden",
    "Golden",
    "Hour",
    "Satellite",
    "Heart",
    "Wildfire",
    "Ocean",
    "Window",
    "Echo",
    "Spiral",
    "Blue",
    "Orbit",
    "Feather",
    "Comet",
    "Dust",
    "Neon",
    "River",
    "Weekend",
    "Postcard",
    "Drift",
    "Honey",
    "Wires",
    "Snow",
    "Fever",
    "Signal",
)

OBSESSION_ARTIST = "Lumen Drift"
OBSESSION_TRACK = "Paper Lanterns"
OBSESSION_WEEK = (date(2025, 3, 10), date(2025, 3, 16))

PODCASTS: tuple[str, ...] = ("The Owl Hour", "Small Data Talk", "Slow History")
PLATFORMS: tuple[str, ...] = ("ios", "macos", "web")

# -- money -------------------------------------------------------------------------------------

EMPLOYERS: tuple[tuple[date, str, int], ...] = (
    (_START, "Pebble Systems SRL", 9800),
    (date(2025, 2, 1), "Northwind Labs SRL", 12500),
)


@dataclass(frozen=True)
class Subscription:
    merchant: str
    merchant_raw: str
    day: int
    amount: str
    currency: str
    active: tuple[date, date]


SUBSCRIPTIONS: tuple[Subscription, ...] = (
    Subscription(
        "Netflix", "NETFLIX.COM AMSTERDAM", 3, "49.99", "RON", (_START, date(2025, 3, 31))
    ),
    Subscription("Netflix", "NETFLIX.COM AMSTERDAM", 3, "59.99", "RON", (date(2025, 4, 1), _END)),
    Subscription("Spotify", "SPOTIFY P2B4C STOCKHOLM", 15, "26.99", "RON", (_START, _END)),
    Subscription("iCloud", "APPLE.COM/BILL ICLOUD", 21, "0.99", "EUR", (_START, _END)),
    Subscription(
        "Stride Gym", "STRIDE GYM BUCURESTI", 1, "180.00", "RON", (_START, date(2025, 6, 30))
    ),
    Subscription(
        "Boulder Barn", "BOULDER BARN CLIMBING", 1, "220.00", "RON", (date(2024, 9, 1), _END)
    ),
    Subscription(
        "YouTube Premium", "GOOGLE *YOUTUBEPREMIUM", 8, "6.99", "EUR", (date(2024, 5, 1), _END)
    ),
)

# (merchant, merchant_raw, category, min, max, weight)
CARD_MERCHANTS: tuple[tuple[str, str, str, int, int, float], ...] = (
    ("Mega Image", "MEGA IMAGE 0421", "groceries", 18, 160, 6),
    ("Lidl", "LIDL DISCOUNT SRL", "groceries", 40, 280, 4),
    ("Kaufland", "KAUFLAND 3310", "groceries", 60, 420, 2),
    ("Carrefour", "CARREFOUR EXPRESS", "groceries", 12, 90, 2),
    ("Glovo", "GLOVO*FOOD ORDER", "food_delivery", 45, 140, 3),
    ("Bolt Food", "BOLT.EU/O/FOOD", "food_delivery", 40, 120, 2),
    ("Tazz", "TAZZ BY EMAG", "food_delivery", 50, 150, 1.5),
    ("Bolt", "BOLT.EU/O/RIDE", "transport", 14, 48, 3),
    ("Metrorex", "METROREX CARD", "transport", 5, 10, 3),
    ("Origami Coffee", "ORIGAMI COFFEE", "eating_out", 12, 28, 4),
    ("Bistro Lantern", "BISTRO LANTERN", "eating_out", 70, 260, 1.5),
    ("Pizza Nebula", "PIZZA NEBULA", "eating_out", 50, 180, 1.2),
    ("Cinema Orbit", "CINEMA ORBIT", "entertainment", 30, 90, 0.6),
    ("Bookshop Quill", "CARTURESTI QUILL", "shopping", 40, 210, 0.6),
    ("eMAG", "EMAG.RO", "shopping", 60, 900, 0.8),
    ("Farmacia Verde", "FARMACIA VERDE", "health", 15, 140, 1),
    ("Petrom", "PETROM 1182", "transport", 150, 320, 0.4),
)

WEEKEND_MERCHANTS: frozenset[str] = frozenset(
    {"Glovo", "Bolt Food", "Tazz", "Bistro Lantern", "Pizza Nebula", "Cinema Orbit", "Bolt"}
)

TRAVEL: tuple[tuple[date, date, str], ...] = (
    (date(2024, 5, 17), date(2024, 5, 21), "Lisbon"),
    (date(2024, 8, 9), date(2024, 8, 18), "Valencia"),
    (date(2025, 4, 25), date(2025, 4, 29), "Vienna"),
    (date(2025, 7, 25), date(2025, 8, 3), "Crete"),
    (date(2026, 6, 12), date(2026, 6, 16), "Ljubljana"),
)
TRAVEL_MERCHANTS: tuple[tuple[str, str, int, int], ...] = (
    ("Café Lumière", "eating_out", 4, 18),
    ("Hotel Meridiana", "travel", 90, 160),
    ("Tram Ticket", "transport", 2, 6),
    ("Mercado Azul", "groceries", 8, 45),
    ("Trattoria Sole", "eating_out", 25, 70),
    ("Museum Pass", "entertainment", 12, 30),
)

# -- browsing and shell ------------------------------------------------------------------------

VIDEO_TOPICS: tuple[str, ...] = (
    "Beginner bouldering technique",
    "How DuckDB executes a query",
    "Lo-fi beats to study to",
    "Easy weeknight pasta",
    "Why the sky is blue",
    "Top 10 board games of the year",
    "Learn Rust in 30 minutes",
    "Morning stretch routine",
    "Cat compilation",
    "Travel vlog: a week by the sea",
    "Climbing finger strength explained",
    "Tiny house tour",
    "Python async explained",
    "Sourdough for beginners",
    "Space documentary",
)

SEARCHES: tuple[str, ...] = (
    "duckdb window functions",
    "weather tomorrow",
    "cheap flights lisbon",
    "python zoneinfo dst",
    "how long to boil an egg",
    "best climbing shoes beginners",
    "pydantic v2 validators",
    "train schedule",
    "pizza near me",
    "typer rich progress bar",
    "what is a sherd",
    "git rebase onto",
    "how to fix a leaking tap",
    "concerts this weekend",
    "sourdough starter ratio",
    "rust lifetimes explained",
    "pharmacy open now",
    "vienna museums",
)

SITES: tuple[tuple[str, str], ...] = (
    ("https://docs.python.org/3/library/zoneinfo.html", "zoneinfo — IANA time zone support"),
    ("https://duckdb.org/docs/sql/functions/window_functions", "Window Functions - DuckDB"),
    ("https://github.com/notifications", "Notifications · GitHub"),
    ("https://en.wikipedia.org/wiki/Potsherd", "Potsherd - Wikipedia"),
    ("https://news.example.com/frontpage", "Front page | Example News"),
    ("https://shop.example.org/cart", "Your cart"),
    ("https://mail.example.net/inbox", "Inbox"),
    ("https://recipes.example.com/pasta", "Easy weeknight pasta"),
    ("https://stackoverflow.com/questions/tagged/python", "Newest 'python' Questions"),
    ("https://maps.example.org/", "Maps"),
    ("https://weather.example.com/today", "Today's forecast"),
    ("https://blog.example.net/posts/local-first", "Local-first software"),
)

SHELL_COMMANDS: tuple[str, ...] = (
    "git status",
    "git diff",
    "git commit -m 'wip'",
    "git push",
    "git pull --rebase",
    "ls -la",
    "cd ~/code/sherd",
    "uv run pytest",
    "uv sync",
    "scripts/check",
    "make build",
    "docker compose up -d",
    "vim notes.md",
    "python -m http.server",
    "htop",
    "brew upgrade",
    "cargo build --release",
    "ssh build-box.test",
    "grep -rn TODO src",
    "duckdb demo.duckdb",
)
