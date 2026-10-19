"""Identity of the parser inputs that produced persisted SourceArtifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions
from jonex_wiki.domain.engine.compile.source_prep.inspect import InspectionPolicy


@lru_cache(maxsize=1)
def parser_code_checksum() -> str:
    parsing_dir = Path(__file__).resolve().parent
    source_prep_dir = parsing_dir.parent / "engine" / "compile" / "source_prep"
    digest = hashlib.sha256()
    for root in (parsing_dir, source_prep_dir):
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root.parent).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def parse_input_signature(settings) -> str:
    """Hash parse settings and installed parser code, never credentials."""
    payload = {
        "schema": 1,
        "code": parser_code_checksum(),
        "packing": asdict(PackingOptions.from_settings(settings)),
        "inspection": asdict(InspectionPolicy.from_settings(settings)),
        "text_encoding": settings.text_encoding,
        "mineru_model_version": settings.mineru_model_version,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
