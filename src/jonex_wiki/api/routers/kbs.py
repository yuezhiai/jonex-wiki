"""平台集成:KB 删除端点(软删全部文档 + 清任务/提示/查询任务 + 删目录)。

裸 kb 路径,故用 prefix="/api/v1"(照 tasks.py),不能挂 "/api/v1/kbs/{kb_id}"。
"""

from fastapi import APIRouter, Depends, Request

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1", tags=["kbs"])


@router.get("/kbs/{kb_id}/compilation")
async def compilation_state(kb_id: str, request: Request, tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.compilation_state(tenant_id, kb_id)


@router.post("/kbs/{kb_id}/compilation/{action}")
async def control_compilation(kb_id: str, action: str, request: Request,
                             tenant_id: str = Depends(require_tenant)):
    from jonex_wiki.kernel.errors import ValidationError
    if action not in ("pause", "resume"):
        raise ValidationError("action 必须为 pause 或 resume")
    return await request.app.state.app.set_compilation_paused(tenant_id, kb_id, action == "pause")


@router.delete("/kbs/{kb_id}", status_code=202)
async def delete_kb(kb_id: str, request: Request, tenant_id: str = Depends(require_tenant)):
    """删除 KB:软删全部文档 + 清 tasks/prompts/query_tasks + 删 {data_root}/{tenant}/{kb}/。

    幂等:KB 不存在(无任何行)也返回 deleted,便于平台重试。
    """
    return await request.app.state.app.delete_kb(tenant_id, kb_id)
