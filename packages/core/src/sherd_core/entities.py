"""Conservative identity resolution with persistent user decisions."""

import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from itertools import combinations
from typing import Literal

from sherd_core.store import Store


def normalise_name(name: str) -> str:
    """Casefold, strip accents and nonletters, collapse whitespace; preserve order."""
    text = unicodedata.normalize("NFKD", name.casefold())
    return " ".join(
        "".join(
            c if c.isalnum() or c.isspace() else " " for c in text if not unicodedata.combining(c)
        ).split()
    )


def normalise_phone(value: str) -> str | None:
    """International phone identities compare by E.164 digits."""
    value = value.removeprefix("whatsapp:")
    if not re.fullmatch(r"\+[\d\s().-]+", value):
        return None
    return "".join(c for c in value if c.isdigit())


def score(a: str, b: str) -> float:
    """Return the decided name/phone score; zero means no candidate."""
    phone_a, phone_b = normalise_phone(a), normalise_phone(b)
    if phone_a or phone_b:
        return 1.0 if phone_a and phone_a == phone_b else 0.0
    a, b = normalise_name(a), normalise_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if ta == tb:
        return 0.95
    if ta < tb or tb < ta:
        return 0.70
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return ratio if ratio >= 0.85 else 0.0


@dataclass(frozen=True)
class Proposal:
    a: str
    b: str
    score: float


@dataclass(frozen=True)
class ResolveReport:
    auto_merged: int
    proposals: tuple[Proposal, ...]
    assigned_messages: int
    assigned_transactions: int


@dataclass
class _Identity:
    names: Counter[str] = field(default_factory=Counter)
    whatsapp_names: Counter[str] = field(default_factory=Counter)
    sources: set[str] = field(default_factory=set)


def resolve(store: Store) -> ResolveReport:
    """Rebuild contacts from distinct identities, without loading individual fact rows.

    Ids and timestamps survive unchanged snapshots. Rejections constrain whole clusters,
    preventing an indirect automatic merge through a third identity.
    """
    identities: dict[str, _Identity] = {}
    rows = store.query("""SELECT sender_id, sender_name, source, count(*) AS n
        FROM messages WHERE NOT is_from_me AND sender_id IS NOT NULL
        GROUP BY sender_id, sender_name, source""").to_pylist()
    for row in rows:
        item = identities.setdefault(row["sender_id"], _Identity())
        item.sources.add(row["source"])
        if row["sender_name"]:
            item.names[row["sender_name"]] += row["n"]
            if row["source"] == "whatsapp":
                item.whatsapp_names[row["sender_name"]] += row["n"]
    parties: dict[str, str] = {}
    for row in store.query("""SELECT counterparty, source FROM transactions
        WHERE kind = 'transfer' AND counterparty IS NOT NULL
        GROUP BY counterparty, source""").to_pylist():
        name = row["counterparty"]
        if not normalise_name(name):
            continue
        key = "bank:name:" + " ".join(sorted(normalise_name(name).split()))
        parties[name] = key
        item = identities.setdefault(key, _Identity())
        item.names[name] += 1
        item.sources.add(row["source"])
    decisions = {
        (r["a"], r["b"]): r["decision"]
        for r in store.query("SELECT a, b, decision FROM contact_decisions").to_pylist()
    }
    groups = {key: {key} for key in identities}
    owners = {key: key for key in identities}

    def join(a: str, b: str, *, confirmed: bool = False) -> bool:
        ga, gb = owners[a], owners[b]
        if ga == gb:
            return False
        if not confirmed:
            for x in groups[ga]:
                for y in groups[gb]:
                    if decisions.get((min(x, y), max(x, y))) == "reject":
                        return False
                    px, py = normalise_phone(x), normalise_phone(y)
                    if px and py and px != py and identities[x].sources & identities[y].sources:
                        return False
        groups[ga].update(groups.pop(gb))
        for key in groups[ga]:
            owners[key] = ga
        return True

    for (a, b), decision in sorted(decisions.items()):
        if decision == "merge" and a in identities and b in identities:
            join(a, b, confirmed=True)
    candidates: list[Proposal] = []
    for a, b in combinations(sorted(identities), 2):
        if decisions.get((a, b)) == "reject":
            continue
        value = max(
            [score(a, b) if normalise_phone(a) and normalise_phone(b) else 0.0]
            + [score(x, y) for x in identities[a].names for y in identities[b].names]
        )
        if value >= 0.70:
            candidates.append(Proposal(a, b, value))
    old = store.query("SELECT * FROM contacts").to_pylist()
    old_by_id = {r["id"]: r for r in old}
    old_owner = {key: r["id"] for r in old if r["merged_into"] is None for key in r["identities"]}
    auto = 0
    for candidate in sorted(candidates, key=lambda p: (-p.score, p.a, p.b)):
        if (
            candidate.score >= 0.95
            and join(candidate.a, candidate.b)
            and (
                old_owner.get(candidate.a) != old_owner.get(candidate.b)
                or candidate.a not in old_owner
            )
        ):
            auto += 1
    proposals = tuple(p for p in candidates if owners[p.a] != owners[p.b])
    now = datetime.now(UTC)
    contacts: list[dict[str, object]] = []
    assignments: dict[str, str] = {}
    claimed: set[str] = set()
    for keys in sorted(groups.values(), key=lambda g: sorted(g)):
        previous = sorted(
            {old_owner[k] for k in keys if k in old_owner} - claimed,
            key=lambda cid: (old_by_id[cid]["created_at"], cid),
        )
        cid = previous[0] if previous else uuid.uuid4().hex
        claimed.add(cid)
        names: Counter[str] = Counter()
        wa: Counter[str] = Counter()
        sources: set[str] = set()
        for key in sorted(keys):
            names.update(identities[key].names)
            wa.update(identities[key].whatsapp_names)
            sources.update(identities[key].sources)
            assignments[key] = cid
        preferred = wa or names
        display = sorted(preferred, key=lambda n: (-preferred[n], n))[0] if preferred else min(keys)
        contact: dict[str, object] = dict(
            id=cid,
            display_name=display,
            aliases=sorted(names),
            identities=sorted(keys),
            sources=sorted(sources),
            merged_into=None,
            created_at=old_by_id[cid]["created_at"] if cid in old_by_id else now,
            updated_at=now,
        )
        if cid in old_by_id and all(
            contact[k] == old_by_id[cid][k] for k in contact if k != "updated_at"
        ):
            contact["updated_at"] = old_by_id[cid]["updated_at"]
        contacts.append(contact)
    for row in old:
        if row["id"] not in claimed:
            target = next((assignments[k] for k in row["identities"] if k in assignments), None)
            if row["merged_into"] != target:
                row = dict(row, merged_into=target, updated_at=now)
            contacts.append(row)
    store.replace_contacts(contacts)
    messages, transactions = store.assign_contacts(
        assignments, {name: assignments[key] for name, key in parties.items()}
    )
    return ResolveReport(auto, proposals, messages, transactions)


def decide(store: Store, a: str, b: str, decision: Literal["merge", "reject"]) -> ResolveReport:
    """Accept identity keys or current contact ids and persist all cross-pair decisions."""
    contacts = {r["id"]: r for r in store.query("SELECT * FROM contacts").to_pylist()}
    known = {k for r in contacts.values() for k in r["identities"]}

    def keys(value: str) -> list[str]:
        if value in contacts and contacts[value]["merged_into"] is None:
            return list(contacts[value]["identities"])
        if value in known:
            return [value]
        raise ValueError("unknown or superseded contact/identity")

    aa, bb = keys(a), keys(b)
    if set(aa) & set(bb):
        raise ValueError("choose distinct contacts/identities")
    for x in aa:
        for y in bb:
            store.decide_contacts(x, y, decision)
    return resolve(store)
