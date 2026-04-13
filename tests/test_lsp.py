from __future__ import annotations

import asyncio
from pathlib import Path
import unittest

from src.features.lsp import LspClient


class _DummyProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None


class _FakeRpc:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, object]] = []
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[object]] = {}

    async def start(self) -> None:
        return

    async def close(self) -> None:
        return

    def on_notification(self, _method: str, _handler) -> None:
        return

    async def send_notification(self, method: str, params: object) -> None:
        self.notifications.append((method, params))
        if method == "$/cancelRequest" and isinstance(params, dict):
            request_id = params.get("id")
            if isinstance(request_id, int):
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_exception(RuntimeError("cancelled"))

    def send_request_with_id(self, method: str, params: object) -> tuple[int, asyncio.Future[object]]:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        if method == "textDocument/completion":
            if request_id == 1:
                async def _slow() -> None:
                    await asyncio.sleep(0.2)
                    if not future.done():
                        future.set_result({"items": [{"label": "slowItem"}]})

                asyncio.create_task(_slow())
            else:
                future.set_result({"items": [{"label": "fastItem"}]})
        else:
            future.set_result({})
        return request_id, future

    async def request(self, method: str, _params: object) -> object:
        if method == "textDocument/codeAction":
            return [
                {
                    "title": "Fake quick fix",
                    "command": {"command": "fake.fix", "arguments": [1]},
                }
            ]
        if method == "workspace/executeCommand":
            return {"ok": True}
        return {}


class LspClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_cancels_previous_request(self) -> None:
        client = LspClient()
        fake_rpc = _FakeRpc()
        client._rpc = fake_rpc  # type: ignore[attr-defined]
        client._process = _DummyProcess()  # type: ignore[attr-defined]
        path = Path("demo.py")

        first = asyncio.create_task(client.completion(path, 0, 0))
        await asyncio.sleep(0.03)
        second_result = await client.completion(path, 0, 1)
        first_result = await first

        self.assertEqual(first_result, [])
        self.assertEqual(second_result, ["fastItem"])
        self.assertTrue(any(method == "$/cancelRequest" for method, _ in fake_rpc.notifications))

    async def test_code_action_and_execute_command(self) -> None:
        client = LspClient()
        fake_rpc = _FakeRpc()
        client._rpc = fake_rpc  # type: ignore[attr-defined]
        client._process = _DummyProcess()  # type: ignore[attr-defined]
        path = Path("demo.py")

        actions = await client.code_actions(path, 0, 0, diagnostics=[])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].get("title"), "Fake quick fix")

        result = await client.execute_command("fake.fix", [1])
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
