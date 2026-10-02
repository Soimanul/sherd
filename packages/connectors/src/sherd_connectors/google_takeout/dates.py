"""Takeout's localised activity dates, resolved without process locale changes."""

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MONTHS = {
    name: month
    for month, names in enumerate(
        (
            "jan january ian ianuarie januar enero ene janvier janv",
            "feb february februarie februar febrero février févr",
            "mar march martie mär märz marzo mars",
            "apr april aprilie abril avr avril",
            "may mai mayo",
            "jun june iun iunie juni junio juin",
            "jul july iul iulie juli julio juillet juil",
            "aug august agosto août",
            "sep sept september septembrie septiembre septiembre septembre",
            "oct october octombrie okt oktober octubre octobre",
            "nov november noiembrie noviembre novembre",
            "dec december decembrie dez dezember diciembre dic décembre déc",
        ),
        1,
    )
    for name in names.split()
}
ZONES = {
    name: hours
    for hours, names in (
        (0, "UTC GMT WET"),
        (1, "CET MEZ BST WEST"),
        (2, "EET CEST MESZ"),
        (3, "EEST"),
        (-5, "EST CDT"),
        (-4, "EDT"),
        (-6, "CST MDT"),
        (-7, "MST PDT"),
        (-8, "PST"),
    )
    for name in names.split()
}


def localise(value: datetime, zone: ZoneInfo) -> datetime:
    candidate = value.replace(tzinfo=zone, fold=0)
    # A spring gap round-trip advances by the transition's size; folds use fold=0.
    return candidate.astimezone(UTC).astimezone(zone).astimezone(UTC)


def parse_date(text: str, zone: ZoneInfo) -> datetime:
    text = text.replace("\u202f", " ").replace("\xa0", " ").strip()
    clock = re.search(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(AM|PM)?", text, re.I)
    if clock is None:
        raise ValueError("unsupported activity timestamp")
    hour, minute, second = (int(clock[i]) for i in (1, 2, 3))
    if clock[5]:
        hour = hour % 12 + (12 if clock[5].upper() == "PM" else 0)
    fraction = int((clock[4] or "").ljust(6, "0")[:6] or "0")
    date_text = text[: clock.start()].strip(" ,")
    numeric = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_text)
    if numeric:
        day, month, year = map(int, numeric.groups())
    else:
        parts = date_text.lower().replace(",", "").replace(".", "").split()
        if len(parts) != 3:
            raise ValueError("unsupported activity date")
        if parts[0] in MONTHS:
            month, day, year = MONTHS[parts[0]], int(parts[1]), int(parts[2])
        else:
            day, month, year = int(parts[0]), MONTHS[parts[1]], int(parts[2])
    value = datetime(year, month, day, hour, minute, second, fraction)
    abbreviation = text[clock.end() :].strip().upper()
    if abbreviation in ZONES:
        return value.replace(tzinfo=timezone(timedelta(hours=ZONES[abbreviation]))).astimezone(UTC)
    return localise(value, zone)
