from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import threading
from typing import Any

from .async_runtime import AsyncRuntime


@dataclass(slots=True)
class ProcessState:
    process_id: int
    command: str
    process: asyncio.subprocess.Process
    output: deque[str] = field(default_factory=deque)
    exited: bool = False
    return_code: int | None = None


class AsyncProcessManager:
    def __init__(self, runtime: AsyncRuntime) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._next_id = 1
        self._states: dict[int, ProcessState] = {}

    def start(self, command: str, *, cwd: str | None = None) -> int:
        process_id = self._allocate_id()
        coro = self._start_process(process_id, command, cwd)
        self._runtime.run_sync(coro, timeout=5.0)
        return process_id

    def write(self, process_id: int, data: str) -> bool:
        if process_id not in self._states:
            return False
        coro = self._write(process_id, data)
        self._runtime.submit(f"proc-write:{process_id}", coro)
        return True

    def stop(self, process_id: int) -> bool:
        if process_id not in self._states:
            return False
        coro = self._stop(process_id)
        self._runtime.submit(f"proc-stop:{process_id}", coro)
        return True

    def status(self, process_id: int) -> str:
        state = self._states.get(process_id)
        if state is None:
            return "unknown"
        if not state.exited:
            return "running"
        return f"exited:{state.return_code}"

    def read(self, process_id: int, *, max_lines: int = 20) -> list[str]:
        state = self._states.get(process_id)
        if state is None:
            return []
        lines: list[str] = []
        limit = max(1, max_lines)
        while state.output and len(lines) < limit:
            lines.append(state.output.popleft())
        return lines

    async def _start_process(self, process_id: int, command: str, cwd: str | None) -> None:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        state = ProcessState(process_id=process_id, command=command, process=process)
        with self._lock:
            self._states[process_id] = state

        asyncio.create_task(self._pump_stream(process_id, process.stdout, "OUT"))
        asyncio.create_task(self._pump_stream(process_id, process.stderr, "ERR"))
        asyncio.create_task(self._wait_for_exit(process_id))

    async def _write(self, process_id: int, data: str) -> None:
        state = self._states.get(process_id)
        if state is None or state.process.stdin is None or state.exited:
            return
        payload = data
        if not payload.endswith("\n"):
            payload = payload + "\n"
        state.process.stdin.write(payload.encode("utf-8"))
        await state.process.stdin.drain()

    async def _stop(self, process_id: int) -> None:
        state = self._states.get(process_id)
        if state is None or state.exited:
            return
        state.process.terminate()

    async def _wait_for_exit(self, process_id: int) -> None:
        state = self._states.get(process_id)
        if state is None:
            return
        code = await state.process.wait()
        state.exited = True
        state.return_code = code
        self._runtime.post_event(
            {
                "type": "process_exit",
                "process_id": process_id,
                "return_code": code,
            }
        )

    async def _pump_stream(
        self,
        process_id: int,
        stream: asyncio.StreamReader | None,
        prefix: str,
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            state = self._states.get(process_id)
            if state is None:
                continue
            line = f"{prefix}> {text}"
            state.output.append(line)
            if len(state.output) > 500:
                state.output.popleft()
            self._runtime.post_event(
                {
                    "type": "process_output",
                    "process_id": process_id,
                    "line": line,
                }
            )

    def _allocate_id(self) -> int:
        with self._lock:
            process_id = self._next_id
            self._next_id += 1
        return process_id
