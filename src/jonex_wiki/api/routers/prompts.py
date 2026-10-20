from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from jonex_wiki.api.deps import require_tenant
from jonex_wiki.domain.prompts import (DEFAULT_PROMPTS, PUBLIC_PROMPT_KEYS, PromptRepo,
                                       resolve_prompt, template_sha)
from jonex_wiki.kernel.errors import ValidationError

router = APIRouter(tags=["prompts"])


class PutPromptBody(BaseModel):
    template: str
    expected_version: int


def _require_public(prompt_key: str) -> None:
    """API 只暴露第一层（对外可改）；内部冻结 key 不进 API。"""
    if prompt_key not in PUBLIC_PROMPT_KEYS:
        raise ValidationError(f"未知 prompt_key: {prompt_key}")


def _to_item(key: str, rp) -> dict:
    return {"prompt_key": key, "source": rp.source, "version": rp.version,
            "template_sha": rp.template_sha, "updated_at": None, "template": rp.template}


@router.get("/api/v1/kbs/{kb_id}/prompts")
async def list_prompts(kb_id: str, request: Request,
                       tenant_id: str = Depends(require_tenant)):
    sf = request.app.state.session_factory
    async with sf() as s:
        items = [_to_item(k, await resolve_prompt(s, tenant_id, kb_id, k))
                 for k in PUBLIC_PROMPT_KEYS]
    return {"kb_id": kb_id, "prompts": items}


@router.put("/api/v1/kbs/{kb_id}/prompts/{prompt_key}")
async def put_prompt(kb_id: str, prompt_key: str, body: PutPromptBody, request: Request,
                     tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.put_prompt(tenant_id, kb_id, prompt_key,
                                                  body.template, body.expected_version)


@router.delete("/api/v1/kbs/{kb_id}/prompts/{prompt_key}", status_code=204)
async def delete_prompt(kb_id: str, prompt_key: str, request: Request,
                        tenant_id: str = Depends(require_tenant)):
    _require_public(prompt_key)
    await request.app.state.app.delete_prompt(tenant_id, kb_id, prompt_key)


@router.get("/api/v1/prompts/defaults")
async def get_defaults(request: Request,
                       tenant_id: str = Depends(require_tenant)):
    return {"prompts": [
        {"prompt_key": k, "version": None, "template_sha": template_sha(DEFAULT_PROMPTS[k]),
         "template": DEFAULT_PROMPTS[k]}
        for k in PUBLIC_PROMPT_KEYS]}
