"""平台集成:只读页面读取端点(从已发布 bundle 读 Wiki 页面树/单页/反向链接/claim)。

bundle 布局:``compile/published`` symlink → ``versions/{bundle_sha}``,内含
``query/navigation.json``(page_index/page_graph)、``indexes/links.json``、
``claims.jsonl`` 与 ``wiki/{page_path}``。本模块只读,不改动 bundle。

读取语义全部下沉到 ``WikiApplication``(见 ``jonex_wiki.domain.reading``),
本模块只做 HTTP 投影。
"""

from fastapi import APIRouter, Depends, Request

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["pages"])


@router.get("/pages")
async def list_pages(kb_id: str, request: Request,
                     tenant_id: str = Depends(require_tenant)):
    """已发布 Wiki 的阅读树页面清单(完整 PageIndexEntry 元数据 + type)。"""
    return await request.app.state.app.list_pages(tenant_id, kb_id)


@router.get("/pages/{page_id:path}/backlinks")
async def get_page_backlinks(kb_id: str, page_id: str, request: Request,
                             tenant_id: str = Depends(require_tenant)):
    """「链入本页」:反向链接 + 正文提及(编译期索引)。"""
    return await request.app.state.app.get_page_backlinks(tenant_id, kb_id, page_id)


@router.get("/pages/{page_id:path}/claims")
async def get_page_claims(kb_id: str, page_id: str, request: Request,
                          tenant_id: str = Depends(require_tenant)):
    """页面级 claim 溯源明细。"""
    return await request.app.state.app.get_page_claims(tenant_id, kb_id, path=page_id)


@router.get("/pages/{page_id:path}")
async def get_page(kb_id: str, page_id: str, request: Request,
                   tenant_id: str = Depends(require_tenant)):
    """读取单页(结构化 index/page/body/content 别名/references)。"""
    return await request.app.state.app.read_page(tenant_id, kb_id, page_id)


@router.get("/claims")
async def get_claims(kb_id: str, request: Request, document_id: str = "",
                     tenant_id: str = Depends(require_tenant)):
    """按源文档过滤的 claim 溯源明细。"""
    return await request.app.state.app.get_page_claims(
        tenant_id, kb_id, document_id=document_id
    )
