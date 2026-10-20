"""Compile request construction.

Extracted verbatim from ``原引擎/algorithm1.py``: the bounded
two-message request builder (``_messages``) and the single structured model
call (``_call``) used by the compile planner and batch acceptance.  The old
import path stays available through the re-export shim.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from jonex_wiki.domain.prompts import COMPILE_MAIN_SKELETON
from jonex_wiki.kernel.ports import ModelPort
from jonex_wiki.kernel.runtime_env import current_env



# ──────────────────────────────────────────────
#  Request construction
# ──────────────────────────────────────────────


def _messages(payload: Dict, compile_main_prompt: str = "") -> List[Dict]:
    """Build the two-message request for one structured model call.

    ``compile_main_prompt`` 是「固定骨架 + 用户编译偏好 suffix」的完整 system
    prompt（由 compile_task 在编译前 resolve 后传入）；为空时回退到固定骨架
    ``COMPILE_MAIN_SKELETON``，保证编译正确性硬约束始终在场。
    """
    payload = dict(payload)
    if isinstance(payload.get("passage"), dict):
        passage = dict(payload["passage"])
        allowed = {"source_name", "source_location", "source_locations", "block_type", "section_path",
                   "table_id", "row_range", "header_source", "qualifier_sources", "table_partial", "parser_id", "parser_version"}
        passage["source_metadata"] = {k: v for k, v in passage.get("source_metadata", {}).items() if k in allowed}
        if "source_locations" in passage["source_metadata"]:
            passage["source_metadata"]["source_locations"] = [
                {k: v for k, v in ref.items() if k in {"position", "page_number", "char_start", "char_end", "bbox"}}
                for ref in passage["source_metadata"]["source_locations"]]
        payload["passage"] = passage
    content = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return [
        {"role": "system", "content": compile_main_prompt or COMPILE_MAIN_SKELETON},
        {"role": "user", "content": content},
    ]


async def _call(
    model: ModelPort,
    payload: Dict,
    schema: Dict,
    max_tokens: int,
    *,
    model_name: Optional[str],
    tenant_id: str,
    context_id: str,
    trace_id: str,
    scene: str | None = None,
    compile_main_prompt: str = "",
) -> Dict:
    from jonex_wiki.kernel.telemetry import count_work
    messages = _messages(payload, compile_main_prompt)
    count_work("compile_model_call")
    count_work("compile_input_characters", sum(len(m["content"]) for m in messages))
    result = await model.complete(
        messages,
        schema,
        max_tokens=max_tokens,
        model=model_name,
        tenant_id=tenant_id,
        context_id=context_id,
        trace_id=trace_id,
        thinking={"type": "disabled"},
        scene=scene,
    )
    count_work("compile_output_characters", len(json.dumps(result, ensure_ascii=False)))
    return result


def compile_output_tokens(explicit: int | None = None) -> int:
    value = explicit if explicit is not None else int(current_env().get("JONEX_WIKI_COMPILE_OUTPUT_TOKENS", "16384"))
    if value <= 0 or value > 32768:
        raise ValueError("compile output budget must be in [1, 32768]")
    return value


def semantic_review_output_tokens(explicit: int | None = None) -> int:
    """Return the bounded semantic-review output budget.

    Large table passages can legitimately produce enough validated claims to
    exceed the historical 4096-token response cap.  Keep the default stable,
    but allow deployments and evaluations to raise it without changing code.
    """
    value = explicit if explicit is not None else int(
        current_env().get("JONEX_WIKI_SEMANTIC_REVIEW_OUTPUT_TOKENS", "4096")
    )
    if value <= 0 or value > 16384:
        raise ValueError("semantic review output budget must be in [1, 16384]")
    return value
