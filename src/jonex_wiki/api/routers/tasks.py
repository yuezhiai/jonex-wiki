from fastapi import APIRouter, Depends, Request

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1", tags=["tasks"])


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request,
                   tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.get_task(tenant_id, task_id)


@router.get("/kbs/{kb_id}/tasks")
async def list_tasks(kb_id: str, request: Request, kind: str | None = None,
                     limit: int = 50, offset: int = 0,
                     tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.list_tasks(tenant_id, kb_id, kind=kind,
                                                  limit=limit, offset=offset)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request,
                      tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.cancel_task(tenant_id, task_id)
