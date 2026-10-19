"""ProgressSink 适配器：engine ProgressSink → executor.set_stage 细粒度阶段。"""

from __future__ import annotations

from jonex_wiki.kernel.contracts import CurationPhase
from jonex_wiki.kernel.ports import ProgressSink


class ProgressSinkAdapter(ProgressSink):
    """把 engine 的 phase/progress_pct/message 持久化到任务行。

    ``executor.set_stage`` 自开短 session 并写入 TaskRow.stage/summary；
    引擎侧 task_id 参数被忽略（同一编译任务只有一个 task 行）。
    """

    def __init__(self, executor, task_id: str) -> None:
        self._executor = executor
        self._task_id = task_id

    async def update(
        self,
        task_id: str,
        phase: CurationPhase,
        progress_pct: int,
        message: str,
    ) -> None:
        await self._executor.set_stage(
            self._task_id,
            phase.value if isinstance(phase, CurationPhase) else str(phase),
            summary={"progress_pct": progress_pct, "message": message},
        )
