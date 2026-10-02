"""Deterministic large extended-history generator (no real account/device data)."""

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path


class SpotifyGenerator:
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        if approx_bytes < 0:
            raise ValueError("approx_bytes must be nonnegative")
        rng = random.Random(seed)
        start = datetime(2024, 1, 1, tzinfo=UTC)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            output.write(b"[\n")
            index, written = 0, 2
            while written < approx_bytes:
                record = {
                    "ts": (start + timedelta(minutes=index)).isoformat(),
                    "platform": "synthetic",
                    "ms_played": rng.randint(1000, 240000),
                    "master_metadata_track_name": f"Fictional Orbit {index % 100}",
                    "master_metadata_album_artist_name": "Fictional Lantern Ensemble",
                    "master_metadata_album_album_name": "Imaginary Skies",
                    "spotify_track_uri": f"spotify:track:synthetic{index % 100}",
                    "episode_name": None,
                    "episode_show_name": None,
                    "spotify_episode_uri": None,
                    "shuffle": bool(index % 2),
                    "skipped": False,
                    "reason_start": "trackdone",
                    "reason_end": "trackdone",
                    "offline": False,
                    "incognito_mode": False,
                    "conn_country": "ZZ",
                }
                line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
                if index:
                    output.write(b",")
                    written += 1
                output.write(line)
                written += len(line)
                index += 1
            output.write(b"]\n")


GENERATOR = SpotifyGenerator()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--bytes", type=int, default=50 * 1024**2)
    parser.add_argument("--seed", type=int, default=5)
    args = parser.parse_args()
    GENERATOR.write(args.path, args.bytes, args.seed)
