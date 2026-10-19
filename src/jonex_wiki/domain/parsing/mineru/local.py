"""Local MinerU CLI backend.

迁移自 core ``parsing/mineru/local.py``。
"""

from __future__ import annotations

import logging
from pathlib import Path

from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult, sha256_file
from .common import MineruOptions, normalize_output, prepare_local_input, run_mineru_cli

logger = logging.getLogger(__name__)


class MineruLocalBackend:
    """Run the installed ``mineru`` command and normalize its files."""

    parser_id = "mineru.local.v1"

    def parse(
        self,
        path: Path,
        *,
        output_dir: Path,
        source_id: str,
        document_id: str,
        options: MineruOptions | None = None,
        source_url: str | None = None,
    ) -> ParseResult:
        if not path.exists():
            raise FileNotFoundError(path)
        options = options or MineruOptions()
        output_dir.mkdir(parents=True, exist_ok=True)
        staging = output_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        input_path = prepare_local_input(path, staging)
        run_mineru_cli(input_path, output_dir, options)
        return normalize_output(
            output_dir,
            source_name=path.name,
            parser_id=self.parser_id,
            source_sha256=sha256_file(path),
            source_id=source_id,
            document_id=document_id,
            source_url=source_url,
            packing=options.packing,
        )

    def check(self) -> bool:
        import shutil

        return shutil.which("mineru") is not None
