"""CancelPort 适配器：engine CancelPort → executor 协作式取消检查点。"""

from __future__ import annotations

from jonex_wiki.kernel.ports import CancelPort


class CancelPortAdapter(CancelPort):
    """把 设计要求 的 task 级取消信号映射成 engine 的同步轮询接口。

    ``executor.check_cancel`` 在标志位置位时抛 ``asyncio.CancelledError``，
    ``raise_if_cancelled`` 原样冒泡；``is_cancelled`` 只读标志不做副作用。
    """

    def __init__(self, executor, task_id: str) -> None:
        self._executor = executor
        self._task_id = task_id

    def raise_if_cancelled(self) -> None:
        self._executor.check_cancel(self._task_id)

    def is_cancelled(self) -> bool:
        return self._executor.is_cancelled(self._task_id)
