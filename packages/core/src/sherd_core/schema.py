"""Canonical DuckDB schema (PLAN §4.2) and its migrations.

A migration is the list of statements that takes the database from `version - 1` to `version`.
Never edit a released migration: add a new version and bump `SCHEMA_VERSION`.
"""

SCHEMA_VERSION = 1

SESSION_SETUP = ("SET TimeZone = 'UTC'",)

_COMMON = """
  id            VARCHAR PRIMARY KEY,
  source        VARCHAR NOT NULL,
  source_file   VARCHAR NOT NULL,
  source_row_id VARCHAR NOT NULL,
  import_id     VARCHAR NOT NULL,
  imported_at   TIMESTAMPTZ NOT NULL,
  meta          JSON,"""

_UNIQUE = "  UNIQUE (source, source_row_id)"

_V1 = (
    f"""
CREATE TABLE messages ({_COMMON}
  chat_id      VARCHAR NOT NULL,
  chat_name    VARCHAR,
  chat_kind    VARCHAR NOT NULL CHECK (chat_kind IN ('direct', 'group')),
  sender_id    VARCHAR,
  sender_name  VARCHAR,
  is_from_me   BOOLEAN NOT NULL,
  contact_id   VARCHAR,
  ts           TIMESTAMPTZ NOT NULL,
  text         VARCHAR,
  kind         VARCHAR NOT NULL CHECK (kind IN ('text', 'media', 'system', 'deleted')),
  media_type   VARCHAR
    CHECK (media_type IN ('image', 'video', 'audio', 'document', 'sticker', 'gif', 'other')),
  reply_to_id  VARCHAR,
{_UNIQUE}
)""",
    f"""
CREATE TABLE media_plays ({_COMMON}
  ts           TIMESTAMPTZ NOT NULL,
  media_kind   VARCHAR NOT NULL CHECK (media_kind IN ('track', 'episode', 'video', 'audiobook')),
  artist       VARCHAR,
  track        VARCHAR,
  album        VARCHAR,
  uri          VARCHAR,
  ms_played    BIGINT NOT NULL,
  platform     VARCHAR,
  shuffle      BOOLEAN,
  skipped      BOOLEAN,
{_UNIQUE}
)""",
    f"""
CREATE TABLE transactions ({_COMMON}
  ts           TIMESTAMPTZ NOT NULL,
  amount       DECIMAL(18, 2) NOT NULL,
  currency     VARCHAR NOT NULL,
  merchant_raw VARCHAR,
  merchant     VARCHAR,
  counterparty VARCHAR,
  contact_id   VARCHAR,
  category     VARCHAR,
  account      VARCHAR NOT NULL,
  balance      DECIMAL(18, 2),
  kind         VARCHAR NOT NULL
    CHECK (kind IN ('card', 'transfer', 'fee', 'topup', 'exchange', 'refund', 'other')),
{_UNIQUE}
)""",
    f"""
CREATE TABLE events ({_COMMON}
  ts           TIMESTAMPTZ NOT NULL,
  kind         VARCHAR NOT NULL,
  title        VARCHAR,
  url          VARCHAR,
{_UNIQUE}
)""",
    f"""
CREATE TABLE locations ({_COMMON}
  ts           TIMESTAMPTZ NOT NULL,
  end_ts       TIMESTAMPTZ,
  lat          DOUBLE NOT NULL,
  lon          DOUBLE NOT NULL,
  accuracy_m   DOUBLE,
  place_name   VARCHAR,
  kind         VARCHAR NOT NULL CHECK (kind IN ('ping', 'visit', 'segment')),
{_UNIQUE}
)""",
    """
CREATE TABLE contacts (
  id           VARCHAR PRIMARY KEY,
  display_name VARCHAR NOT NULL,
  aliases      VARCHAR[] NOT NULL,
  identities   VARCHAR[] NOT NULL,
  sources      VARCHAR[] NOT NULL,
  merged_into  VARCHAR,
  created_at   TIMESTAMPTZ NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL
)""",
    """
CREATE TABLE imports (
  id                VARCHAR PRIMARY KEY,
  connector         VARCHAR NOT NULL,
  connector_version VARCHAR NOT NULL,
  path_hash         VARCHAR NOT NULL,
  tz                VARCHAR NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  rows_seen         BIGINT NOT NULL DEFAULT 0,
  rows_inserted     BIGINT NOT NULL DEFAULT 0,
  status            VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed'))
)""",
    "CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {1: _V1}
