"""Wiki 阅读目录端点（目录功能下沉）。

读 / 起草 / 调整三条都同步返回 view payload（draft/adjust 同步等 LLM，不走 task）；
自动触发（编译后）走独立 outline task，见 ``domain/outline/task.py``。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["outline"])


class DraftOutlineBody(BaseModel):
    model: Optional[str] = Field(default=None, max_length=128)
    extra_instruction: Optional[str] = Field(default=None, max_length=2000)
    unclassified_title: Optional[str] = Field(default=None, max_length=64)


class AdjustOutlineBody(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=2000)
    model: Optional[str] = Field(default=None, max_length=128)
    unclassified_title: Optional[str] = Field(default=None, max_length=64)


@router.get("/outline")
async def get_outline(kb_id: str, request: Request, document_id: str = "",
                      tenant_id: str = Depends(require_tenant)):
    """读目录 + 页面清单（阅读页首屏一次拿全；document_id 非空 = 文档级投影）。"""
    return await request.app.state.app.get_outline(tenant_id, kb_id, document_id)


@router.post("/outline/draft")
async def draft_outline(kb_id: str, body: DraftOutlineBody, request: Request,
                        tenant_id: str = Depends(require_tenant)):
    """LLM 起草目录并落库（人工「重新生成」无条件覆盖旧 active 行）。"""
    return await request.app.state.app.draft_outline(
        tenant_id, kb_id, body.model_dump(exclude_none=True))


@router.post("/outline/adjust")
async def adjust_outline(kb_id: str, body: AdjustOutlineBody, request: Request,
                         tenant_id: str = Depends(require_tenant)):
    """按自然语言指令调整现有目录（覆盖率护栏不过则整次失败）。"""
    return await request.app.state.app.adjust_outline(
        tenant_id, kb_id, body.model_dump(exclude_none=True))
