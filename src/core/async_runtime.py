from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import queue
import threading
from typing import Any


class AsyncRuntime:
    """Dedicated asyncio loop running in a background thread."""

    def __init__(self) -> None:
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="pvim-async-loop", daemon=True)
        self._thread.start()
        self._next_task_id = 1
        self._task_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(self, label: str, coro: Coroutine[Any, Any, Any]) -> int:
        if self._closed or self._loop.is_closed():
            raise RuntimeError("Async runtime is closed.")
        with self._task_lock:
            task_id = self._next_task_id
            self._next_task_id += 1

        wrapped = self._wrap_task(task_id=task_id, label=label, coro=coro)
        asyncio.run_coroutine_threadsafe(wrapped, self._loop)
        return task_id

    def run_sync(self, coro: Coroutine[Any, Any, Any], *, timeout: float = 5.0) -> Any:
        if self._closed or self._loop.is_closed():
            raise RuntimeError("Async runtime is closed.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def poll_events(self, *, max_items: int = 128) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while len(events) < max_items:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def post_event(self, event: dict[str, Any]) -> None:
        self._events.put(event)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_loop(), self._loop)
            future.result(timeout=2.5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            return
        self._thread.join(timeout=3.0)

    async def _wrap_task(self, task_id: int, label: str, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            result = await coro
            self._events.put(
                {
                    "type": "task_done",
                    "task_id": task_id,
                    "label": label,
                    "result": result,
                    "error": "",
                }
            )
        except Exception as exc:
            self._events.put(
                {
                    "type": "task_done",
                    "task_id": task_id,
                    "label": label,
                    "result": None,
                    "error": str(exc),
                }
            )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

    async def _shutdown_loop(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        if pending:
            _done, pending_set = await asyncio.wait(pending, timeout=0.35)
            pending = [task for task in pending_set if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
