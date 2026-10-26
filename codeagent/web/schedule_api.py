"""Timed task HTTP routes; protected by the application's local session middleware."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Response
from pydantic import Field

from codeagent.web.schemas import ApiModel


class ScheduleRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    workspace: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=200_000)
    kind: Literal["once", "interval"]
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000, strict=True)
    read_only: bool = True
    enabled: bool = True


class ScheduleEnabledRequest(ApiModel):
    enabled: bool


def schedule_router(repository, workspace_catalog):
    router = APIRouter(prefix="/api/schedules", tags=["schedules"])

    @router.get("")
    def list_schedules():
        return repository.list_schedules()

    @router.post("", status_code=201)
    def create_schedule(body: ScheduleRequest):
        values = body.model_dump()
        values["workspace"] = str(workspace_catalog.resolve(body.workspace))
        return repository.save_schedule(**values)

    @router.put("/{schedule_id}")
    def update_schedule(schedule_id: str, body: ScheduleRequest):
        values = body.model_dump()
        values["workspace"] = str(workspace_catalog.resolve(body.workspace))
        return repository.save_schedule(schedule_id=schedule_id, **values)

    @router.patch("/{schedule_id}")
    def enable_schedule(schedule_id: str, body: ScheduleEnabledRequest):
        return repository.set_schedule_enabled(schedule_id, body.enabled)

    @router.delete("/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str):
        repository.delete_schedule(schedule_id)
        return Response(status_code=204)

    @router.get("/{schedule_id}/history")
    def history(schedule_id: str, limit: int = Query(default=50, ge=1, le=200)):
        return repository.list_schedule_occurrences(schedule_id, limit=limit)

    return router
