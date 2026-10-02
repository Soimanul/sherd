"""Importing a synthetic profile into a store as one import of connector `synth`."""

import hashlib
from itertools import groupby

from sherd_core import Store, UpsertStats

from sherd_connectors.pipeline import import_rows
from sherd_connectors.synth.generator import TZ, Profile, generate, source_of

CONNECTOR_ID = "synth"
VERSION = "1"


def import_profile(store: Store, profile: Profile, seed: int = 42) -> UpsertStats:
    """Generate the profile and upsert it; each row keeps the source a real export would have."""
    hashed = hashlib.sha256(f"synthetic/{profile}\x1f{seed}".encode()).hexdigest()
    return import_rows(
        store,
        CONNECTOR_ID,
        VERSION,
        hashed,
        TZ.key,
        groupby(generate(profile, seed), key=source_of),
    )
