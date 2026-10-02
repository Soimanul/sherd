"""Settings: connectors and their last import, the LLM provider, privacy, and where data lives.

Read-only, except withdrawing consent for a provider (POST, CSRF), which edits config.json
through the agent's config module.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sherd_agent import config, privacy, providers
from sherd_connectors import registry as connectors
from sherd_core import Store
from sherd_core.paths import default_db_path, sherd_home

from sherd_web.deps import SettingsDep, StoreDep, render
from sherd_web.routes import ask
from sherd_web.routes._pages import Form, shell

router = APIRouter()
logger = logging.getLogger("sherd.web")

# Connectors that write rows but are not registered for `sherd dig`.
UNREGISTERED_NAMES = {"synth": "Synthetic demo data"}


@dataclass(frozen=True)
class ConnectorRow:
    id: str
    name: str
    version: str
    registered: bool
    imports: int = 0
    last_finished: datetime | None = None
    last_rows: int | None = None
    last_tz: str | None = None
    last_version: str | None = None


def connector_rows(store: Store | None) -> tuple[list[ConnectorRow], str | None]:
    """Registered connectors, then any other connector in the import ledger."""
    problem = None
    try:
        registered = connectors.discover()
    except connectors.RegistryError as error:
        registered, problem = {}, str(error)
    latest: dict[str, dict[str, Any]] = {}
    if store is not None:
        for row in store.query(
            """SELECT connector, connector_version, tz, finished_at, rows_inserted,
                      count(*) OVER (PARTITION BY connector) AS imports
               FROM imports WHERE status = 'succeeded'
               QUALIFY row_number() OVER (
                   PARTITION BY connector ORDER BY finished_at DESC NULLS LAST, started_at DESC
               ) = 1"""
        ).to_pylist():
            latest[str(row["connector"])] = row
    rows = []
    for connector_id in [*registered, *sorted(set(latest) - set(registered))]:
        found = registered.get(connector_id)
        last = latest.get(connector_id)
        rows.append(
            ConnectorRow(
                id=connector_id,
                name=found.display_name
                if found
                else UNREGISTERED_NAMES.get(connector_id, connector_id),
                version=str(found.version) if found else "",
                registered=found is not None,
                imports=int(last["imports"]) if last else 0,
                last_finished=last["finished_at"] if last else None,
                last_rows=int(last["rows_inserted"]) if last else None,
                last_tz=str(last["tz"]) if last else None,
                last_version=str(last["connector_version"]) if last else None,
            )
        )
    return rows, problem


@dataclass(frozen=True)
class ProviderRow:
    name: str
    remote: bool
    env: str | None
    key_set: bool
    consented_at: str | None


def provider_rows(consent: dict[str, Any]) -> list[ProviderRow]:
    """Every known provider: its key variable (set or not, never the value) and consent."""
    settings = providers.load_settings()
    order = [*settings.default_order, *sorted(set(providers.NAMES) - set(settings.default_order))]
    rows = []
    for name in order:
        if name == "stub":
            continue
        remote = name in providers.REMOTE
        env = str(settings.get(name).get("api_key_env", f"{name.upper()}_API_KEY"))
        rows.append(
            ProviderRow(
                name=name,
                remote=remote,
                env=env if remote else None,
                key_set=bool(os.environ.get(env)) if remote else False,
                consented_at=str(consent[name]) if name in consent else None,
            )
        )
    return rows


@dataclass(frozen=True)
class Usage:
    name: str
    requests: int
    bytes_sent: int
    bytes_received: int
    input_tokens: int
    output_tokens: int


def usage_rows(section: dict[str, Any]) -> list[Usage]:
    rows = []
    for name, totals in sorted(section.items()):
        if not isinstance(totals, dict):
            continue
        rows.append(Usage(name, *(int(totals.get(key, 0)) for key in privacy.COUNTERS)))
    return rows


def file_size(size: int) -> str:
    """1536 -> '1.5 KB'."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value):,} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, store: StoreDep, settings: SettingsDep) -> Response:
    rows, registry_problem = connector_rows(store)
    problems = [registry_problem] if registry_problem else []
    try:
        consent = dict(config.load().get("consent", {}))
    except ValueError as error:
        consent, problems = {}, [*problems, f"{config.config_path()}: {error}"]
    try:
        counters = privacy.load()
    except ValueError as error:
        counters, problems = (
            {"remote": {}, "local": {}},
            [*problems, f"{privacy.privacy_path()}: {error}"],
        )
    try:
        status = ask.provider_status()
    except ValueError as error:
        status, problems = ask.Status(), [*problems, str(error)]
    remote = usage_rows(counters.get("remote", {}))
    db = settings.db_path
    context = shell(
        store,
        connectors=rows,
        used=[row for row in rows if row.imports],
        problems=problems,
        status=status,
        provider_rows=provider_rows(consent),
        remote=remote,
        local=usage_rows(counters.get("local", {})),
        remote_bytes=sum(row.bytes_sent for row in remote),
        remote_requests=sum(row.requests for row in remote),
        db_exists=db.is_file(),
        db_size=file_size(db.stat().st_size) if db.is_file() else None,
        default_db=default_db_path(),
        home=sherd_home(),
        config_path=config.config_path(),
        privacy_path=privacy.privacy_path(),
    )
    return render(request, "pages/settings.html", context)


@router.post("/settings/consent")
def withdraw_consent(form: Form) -> Response:
    """Forget the consent given to one provider; the next question asks again."""
    provider = form.get("provider", "")
    ask_config = config.load()
    consent = dict(ask_config.get("consent", {}))
    if provider in consent:
        del consent[provider]
        config.save({**ask_config, "consent": consent})
        logger.info("consent withdrawn: provider=%s", provider)
    return RedirectResponse("/settings#llm-title", status_code=303)
