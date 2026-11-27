"""Auto-clip workflow routes.

Watch a YouTube channel and clip every new upload automatically. Workflows are
per workspace: with an active team they belong to the team, otherwise to the
user. Requests carry the frontend's signed session headers or an API key, the
same as the tasks and social routes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth_headers import AuthContext, resolve_auth_context
from ...config import get_config
from ...database import get_db
from ...services.auto_clip_service import AutoClipError, AutoClipService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auto-clip", tags=["auto-clip"])


def _service(db: AsyncSession) -> AutoClipService:
    return AutoClipService(db)


async def _auth_context(request: Request, db: AsyncSession) -> AuthContext:
    return await resolve_auth_context(request, db, get_config())


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - empty or invalid body
        return {}
    return payload if isinstance(payload, dict) else {}


def _http_error(exc: AutoClipError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/")
async def list_workflows(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _auth_context(request, db)
    workflows = await _service(db).list_workflows(ctx.user_id, ctx.team_id)
    return {"workflows": workflows}


@router.post("/")
async def create_workflow(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _auth_context(request, db)
    data = await _json_body(request)
    channel_url = data.get("channel_url")
    if not isinstance(channel_url, str) or not channel_url.strip():
        raise HTTPException(status_code=422, detail="channel_url is required.")
    try:
        workflow = await _service(db).create_workflow(
            ctx.user_id, ctx.team_id, channel_url
        )
    except AutoClipError as exc:
        raise _http_error(exc) from exc
    return {"workflow": workflow}


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    ctx = await _auth_context(request, db)
    data = await _json_body(request)
    if not isinstance(data.get("enabled"), bool):
        raise HTTPException(status_code=422, detail="enabled must be true or false.")
    try:
        workflow = await _service(db).set_enabled(
            workflow_id, ctx.user_id, ctx.team_id, data["enabled"]
        )
    except AutoClipError as exc:
        raise _http_error(exc) from exc
    return {"workflow": workflow}


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    ctx = await _auth_context(request, db)
    try:
        await _service(db).delete_workflow(workflow_id, ctx.user_id, ctx.team_id)
    except AutoClipError as exc:
        raise _http_error(exc) from exc
    return {"deleted": True}
