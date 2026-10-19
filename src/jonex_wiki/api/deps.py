import secrets
import uuid

from fastapi import Header, HTTPException, Request


def make_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:16]}"


async def require_tenant(request: Request,
                         x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                         x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID")) -> str:
    """单一内部 key + 动态租户(方案 B):X-API-Key 常量时间校验调用方身份,租户来自 X-Tenant-ID。

    平台租户动态增删、频率高,不落 key→tenant 静态映射;单一内部 key 校验身份,
    业务租户由适配器透传平台真实租户(经 X-Tenant-ID),引擎不猜测租户。
    """
    expected = request.app.state.settings.api_key
    if not expected:
        raise HTTPException(status_code=500, detail={"code": "internal_error", "message": "JONEX_WIKI_API_KEY 未配置"})
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "X-API-Key 无效"})
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail={"code": "missing_tenant", "message": "缺少 X-Tenant-ID"})
    return x_tenant_id
