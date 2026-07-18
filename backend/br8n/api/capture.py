"""POST /v1/capture — receive a WorkspaceSnapshot from the VS Code extension."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from br8n.agent.state import Principal
from br8n.api.auth import require_principal
from br8n.capture.models import WorkspaceSnapshot
from br8n.capture.service import persist_snapshot
from br8n.interfaces.mcp.tenancy import resolve_tenant
from br8n.knowledge_graph.activity import schedule_activity_update

router = APIRouter(prefix="/v1", dependencies=[Depends(require_principal)])


class SnapshotRequest(BaseModel):
    project: str
    kb: str
    trigger: str
    captured_at: str
    branch: str | None = None
    git_diff_stat: str | None = None
    open_files: list[str] = []
    cursor_file: str | None = None
    cursor_line: int | None = None
    terminal_tail: str | None = None
    hypothesis: str | None = None
    next_action: str | None = None
    thread_id: str | None = None
    project_path: str = ""


class CaptureResponse(BaseModel):
    finding_id: str
    coverage: str


@router.post("/capture", response_model=CaptureResponse)
async def capture(body: SnapshotRequest, principal: Principal = Depends(require_principal)) -> CaptureResponse:
    """Persist a workspace snapshot as a Finding and return the finding id.

    Creates the project and KB on demand (idempotent by name). The coverage
    band returned tells the extension whether the resume card will be rich
    or thin — useful for deciding whether to show a "explore?" affordance.
    """
    snap = WorkspaceSnapshot(
        project_path=body.project_path or body.project,
        trigger=body.trigger,  # type: ignore[arg-type]
        captured_at=body.captured_at,
        branch=body.branch,
        git_diff_stat=body.git_diff_stat,
        open_files=body.open_files,
        cursor_file=body.cursor_file,
        cursor_line=body.cursor_line,
        terminal_tail=body.terminal_tail,
        hypothesis=body.hypothesis,
        next_action=body.next_action,
        thread_id=body.thread_id,
    )
    ctx = resolve_tenant(body.project, body.kb, create=True, principal=principal)
    finding_id = await persist_snapshot(ctx, snap)
    schedule_activity_update(snap, finding_id, access_token=principal.access_token, org_id=principal.org_id)  # fire-and-forget; best-effort
    return CaptureResponse(finding_id=finding_id, coverage="sparse")
