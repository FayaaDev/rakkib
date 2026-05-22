"""HTTP API routes for the Rakkib web runtime."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from rakkib.host_platform import ensure_state_platform
from rakkib.normalize import eval_when
from rakkib.schema import FieldDef, QuestionSchema, load_all_schemas
from rakkib.service_catalog import deployed_service_urls
from rakkib.state import State, default_state_path
from rakkib.steps import load_service_registry

from .answers import PhaseValidationError, apply_phase_answers
from .auth import AuthManager
from .host_auth import check_host_auth_readiness
from .models import WebRuntimeConfig
from .run import WebRunManager

REDACTED_SECRET = "[redacted]"


class SessionBootstrapRequest(BaseModel):
    """Incoming bootstrap token payload."""

    token: Optional[str] = None


class StatePatchRequest(BaseModel):
    """Generic partial state update payload."""

    state: dict[str, object] = {}


class PhaseAnswersRequest(BaseModel):
    """Phase answer submission payload."""

    answers: dict[str, object] = {}
    confirmations: dict[str, bool] = {}


class RunStartRequest(BaseModel):
    """Background run start payload."""

    mode: Optional[str] = None


def _load_state(state_path: Path) -> State:
    """Load the persisted setup state from disk."""
    state = State.load(state_path)
    ensure_state_platform(state)
    return state


def _schemas() -> list[QuestionSchema]:
    """Load all question schemas in phase order."""
    return load_all_schemas()


def _redact_state_payload(state: State) -> dict[str, object]:
    """Return a redacted copy of the persisted state."""
    payload = deepcopy(state.to_dict())
    secrets = payload.get("secrets")

    if not isinstance(secrets, dict):
        return payload

    values = secrets.get("values")
    if not isinstance(values, dict):
        return payload

    for key, value in values.items():
        if value is not None:
            values[key] = REDACTED_SECRET

    return payload


def _redact_record_value(record_key: str, value: object) -> object:
    """Redact a record value when it points at stored secrets."""
    if not record_key.startswith("secrets.values"):
        return value
    if value is None:
        return None
    return REDACTED_SECRET


def _active_fields(schema: QuestionSchema, state: State) -> list[FieldDef]:
    """Return only fields whose `when` expressions are currently active."""
    active: list[FieldDef] = []

    for field in schema.fields:
        if field.when and not eval_when(field.when, state):
            continue
        active.append(field)

    return active


def _field_answers(field: FieldDef, state: State) -> object:
    """Serialize the current answer shape for one active field."""
    if field.type == "summary":
        return {key: _redact_record_value(key, state.get(key)) for key in field.summary_fields}

    if field.type == "secret_group":
        values: dict[str, object] = {}
        for entry in field.entries:
            key = str(entry.get("key", "")).strip()
            when = str(entry.get("when", "always")).strip()

            if not key:
                continue
            if when != "always" and not eval_when(when, state):
                continue

            record_key = f"secrets.values.{key}"
            values[key] = _redact_record_value(record_key, state.get(record_key))
        return values

    if not field.records:
        return None

    if len(field.records) == 1:
        record_key = field.records[0]
        return _redact_record_value(record_key, state.get(record_key))

    return {record_key: _redact_record_value(record_key, state.get(record_key)) for record_key in field.records}


def _serialize_phase_summary(schema: QuestionSchema, state: State) -> dict[str, object]:
    """Build the route metadata for a single setup phase."""
    return {
        "phase": schema.phase,
        "complete": state.is_phase_complete(schema.phase),
        "writes_state": schema.writes_state,
        "has_service_catalog": bool(schema.service_catalog),
        "route": f"/setup/phase/{schema.phase}",
    }


def _service_catalog_category(slug: str, registry: dict[str, object], fallback: str) -> str:
    """Return the registry category used to group services in the web picker."""
    services = registry.get("services", [])
    if not isinstance(services, list):
        return fallback

    by_id = {svc.get("id"): svc for svc in services if isinstance(svc, dict)}
    service = by_id.get(slug) or {}
    homepage = service.get("homepage") if isinstance(service, dict) else {}
    if isinstance(homepage, dict):
        category = str(homepage.get("category") or "").strip()
        if category:
            return category

    return fallback


def _serialize_service_catalog(catalog: dict[str, object]) -> dict[str, object]:
    """Add display metadata to schema service catalog entries."""
    if not catalog:
        return {}

    registry = load_service_registry()
    fallbacks = {
        "foundation_bundle": "Infrastructure",
        "optional_services": "Other",
        "host_addons": "Host Add-ons",
    }
    enriched: dict[str, object] = {}

    for key, value in catalog.items():
        if not isinstance(value, list):
            enriched[key] = value
            continue

        fallback = fallbacks.get(key, "Other")
        enriched[key] = [
            {
                **item,
                "category": _service_catalog_category(str(item.get("slug", "")), registry, fallback),
            }
            if isinstance(item, dict)
            else item
            for item in value
        ]

    return enriched


def _serialize_phase(schema: QuestionSchema, state: State) -> dict[str, object]:
    """Build the API payload for a single setup phase."""
    active_fields = _active_fields(schema, state)

    return {
        "phase": schema.phase,
        "complete": state.is_phase_complete(schema.phase),
        "reads_state": schema.reads_state,
        "writes_state": schema.writes_state,
        "fields": [_serialize_field(field, state) for field in active_fields],
        "service_catalog": _serialize_service_catalog(schema.service_catalog),
        "rules": schema.rules,
        "execution_generated_only": schema.execution_generated_only,
        "answers": {field.id: _field_answers(field, state) for field in active_fields},
    }


def _deployment_succeeded(state: State) -> bool:
    """Return whether a browser-triggered deployment completed successfully."""
    return bool(state.get("deployed.exists")) or state.get("web_deployment.status") == "succeeded"


def _active_service_ids(state: State) -> set[str]:
    """Return services selected through the setup/service picker."""
    active_ids = set(state.get("foundation_services", []) or [])
    active_ids.update(state.get("selected_services", []) or [])
    return active_ids


def _deployed_urls(state: State) -> list[dict[str, str]]:
    """Build user-facing service URLs for the current exposure mode."""
    registry = load_service_registry()
    return deployed_service_urls(state, registry, _active_service_ids(state))


def _serialize_field(field: FieldDef, state: State) -> dict[str, object]:
    """Serialize one field with active secret entries only."""
    payload = asdict(field)
    if field.type != "secret_group":
        return payload

    payload["entries"] = [
        entry
        for entry in field.entries
        if str(entry.get("when", "always")).strip() == "always"
        or eval_when(str(entry.get("when", "always")).strip(), state)
    ]
    return payload


def build_api_router(auth: AuthManager, config: WebRuntimeConfig, run_manager: WebRunManager) -> APIRouter:
    """Create the minimal API router for the current web phase."""
    router = APIRouter()
    state_path = default_state_path(config.repo_dir)

    def serialize_run_state() -> dict[str, object]:
        state = _load_state(state_path)
        snapshot = run_manager.snapshot()
        host_auth = check_host_auth_readiness()
        ready_to_start = state.resume_phase() >= 7 and state.is_confirmed()
        snapshot["resume_phase"] = state.resume_phase()
        snapshot["confirmed"] = state.is_confirmed()
        snapshot["deployment_succeeded"] = _deployment_succeeded(state)
        snapshot["deployed_urls"] = _deployed_urls(state)
        snapshot["host_auth"] = host_auth.to_dict()
        snapshot["can_start"] = bool(snapshot["can_start"]) and ready_to_start and host_auth.ok
        return snapshot

    @router.get("/health")
    def health() -> dict[str, Union[bool, str]]:
        return {
            "ok": True,
            "auth_enabled": auth.token_auth_enabled,
            "mode": "token" if auth.token_auth_enabled else "open",
        }

    @router.post("/session/bootstrap")
    def session_bootstrap(payload: SessionBootstrapRequest, response: Response) -> dict[str, Union[bool, str]]:
        response.headers["Cache-Control"] = "no-store"

        if not auth.validate_token(payload.token):
            response.status_code = 401
            return {
                "ok": False,
                "message": (
                    "This setup token is invalid or expired. Open the printed setup URL again or run "
                    "`rakkib web --lan` to generate a fresh token."
                ),
            }

        session_id, csrf_token = auth.issue_session()
        auth.set_session_cookie(response, session_id)
        return {"ok": True, "csrf_token": csrf_token}

    @router.get("/session")
    def session_status(request: Request, response: Response) -> dict[str, Union[bool, str, None]]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"
        return {
            "authenticated": True,
            "auth_enabled": auth.token_auth_enabled,
            "csrf_token": auth.csrf_token_for_request(request),
        }

    @router.get("/session/bootstrap-token")
    def session_bootstrap_token(request: Request, response: Response) -> dict[str, object]:
        """Return the in-process bootstrap token for authenticated sessions.

        This lets the UI re-render the setup QR code even after refresh/new tab.
        """
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"
        return {
            "token": auth.bootstrap_token(),
            "auth_enabled": auth.token_auth_enabled,
        }

    @router.post("/session/logout")
    def session_logout(request: Request, response: Response) -> dict[str, bool]:
        auth.require_api_auth(request)
        auth.require_csrf(request)
        response.headers["Cache-Control"] = "no-store"
        auth.revoke_session_for_request(request)
        auth.clear_session_cookie(response)
        return {"ok": True}

    @router.get("/state")
    def state_payload(request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"

        state = _load_state(state_path)
        return {
            "state": _redact_state_payload(state),
            "confirmed": state.is_confirmed(),
            "resume_phase": state.resume_phase(),
            "deployment_succeeded": _deployment_succeeded(state),
        }

    @router.patch("/state")
    def patch_state(payload: StatePatchRequest, request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        auth.require_csrf(request)
        response.headers["Cache-Control"] = "no-store"

        if payload.state:
            raise HTTPException(
                status_code=422,
                detail="Use the phase answers API for setup updates; arbitrary state patches are not allowed.",
            )

        state = _load_state(state_path)
        return {
            "state": _redact_state_payload(state),
            "confirmed": state.is_confirmed(),
            "resume_phase": state.resume_phase(),
            "deployment_succeeded": _deployment_succeeded(state),
        }

    @router.get("/state/resume")
    def state_resume(request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"

        state = _load_state(state_path)
        schemas = _schemas()

        return {
            "resume_phase": state.resume_phase(),
            "confirmed": state.is_confirmed(),
            "deployment_succeeded": _deployment_succeeded(state),
            "phases": [_serialize_phase_summary(schema, state) for schema in schemas],
        }

    @router.get("/questions/phases")
    def question_phases(request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"

        state = _load_state(state_path)
        schemas = _schemas()

        return {
            "phases": [_serialize_phase_summary(schema, state) for schema in schemas],
        }

    @router.get("/questions/phases/{phase}")
    def question_phase(phase: int, request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"

        state = _load_state(state_path)
        schema = next((item for item in _schemas() if item.phase == phase), None)
        if schema is None:
            raise HTTPException(status_code=404, detail=f"Unknown phase: {phase}")

        return _serialize_phase(schema, state)

    @router.post("/questions/phases/{phase}/answers")
    def submit_phase_answers(
        phase: int,
        payload: PhaseAnswersRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        auth.require_api_auth(request)
        auth.require_csrf(request)
        response.headers["Cache-Control"] = "no-store"

        current_state = _load_state(state_path)
        schema = next((item for item in _schemas() if item.phase == phase), None)
        if schema is None:
            raise HTTPException(status_code=404, detail=f"Unknown phase: {phase}")

        try:
            updated_state = apply_phase_answers(
                current_state,
                schema,
                payload.answers,
                confirmations=payload.confirmations,
            )
        except PhaseValidationError as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": error.message,
                    "field_errors": error.field_errors,
                },
            ) from error

        updated_state.save(state_path)

        return {
            "ok": True,
            "phase": _serialize_phase(schema, updated_state),
            "resume_phase": updated_state.resume_phase(),
            "confirmed": updated_state.is_confirmed(),
        }

    @router.get("/run")
    def run_status(request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        response.headers["Cache-Control"] = "no-store"
        return serialize_run_state()

    @router.post("/run/start")
    def start_run(request: Request, response: Response, payload: Optional[RunStartRequest] = None) -> dict[str, object]:
        auth.require_api_auth(request)
        auth.require_csrf(request)
        response.headers["Cache-Control"] = "no-store"

        state = _load_state(state_path)
        mode = str((payload.mode if payload else None) or "full_setup")

        if mode == "full_setup":
            if state.resume_phase() < 7:
                raise HTTPException(
                    status_code=409, detail="Complete all setup phases before starting the installer run."
                )
            if not state.is_confirmed():
                raise HTTPException(
                    status_code=409, detail="Phase 6 must be confirmed before starting the installer run."
                )
        elif mode == "service_sync":
            if not _deployment_succeeded(state):
                raise HTTPException(
                    status_code=409,
                    detail="Complete the initial deployment before syncing services from the web dashboard.",
                )
        else:
            raise HTTPException(status_code=422, detail=f"Unknown run mode: {mode}")

        host_auth = check_host_auth_readiness()
        if not host_auth.ok:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": host_auth.message,
                    "host_auth": host_auth.to_dict(),
                },
            )

        try:
            run_manager.start(mode)
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

        return serialize_run_state()

    @router.post("/run/cancel")
    def cancel_run(request: Request, response: Response) -> dict[str, object]:
        auth.require_api_auth(request)
        auth.require_csrf(request)
        response.headers["Cache-Control"] = "no-store"
        run_manager.cancel()
        return serialize_run_state()

    return router
