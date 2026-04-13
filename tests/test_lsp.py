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
        if method == "textDocument/references":
            return [
                {
                    "uri": Path("demo.py").resolve().as_uri(),
                    "range": {"start": {"line": 3, "character": 4}},
                }
            ]
        if method == "textDocument/implementation":
            return [
                {
                    "uri": Path("impl.py").resolve().as_uri(),
                    "range": {"start": {"line": 8, "character": 2}},
                }
            ]
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": "Outer",
                    "kind": 5,
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 10, "character": 0}},
                    "selectionRange": {
                        "start": {"line": 0, "character": 6},
                        "end": {"line": 0, "character": 11},
                    },
                    "children": [
                        {
                            "name": "inner",
                            "kind": 6,
                            "range": {"start": {"line": 2, "character": 0}, "end": {"line": 4, "character": 0}},
                            "selectionRange": {
                                "start": {"line": 2, "character": 8},
                                "end": {"line": 2, "character": 13},
                            },
                        }
                    ],
                }
            ]
        if method == "workspace/symbol":
            return [
                {
                    "name": "GlobalThing",
                    "kind": 12,
                    "containerName": "pkg",
                    "location": {
                        "uri": Path("pkg.py").resolve().as_uri(),
                        "range": {"start": {"line": 15, "character": 1}, "end": {"line": 15, "character": 8}},
                    },
                }
            ]
        if method == "textDocument/rename":
            return {
                "changes": {
                    Path("demo.py").resolve().as_uri(): [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 3},
                            },
                            "newText": "renamed",
                        }
                    ]
                }
            }
        if method == "textDocument/formatting":
            return [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 4},
                    },
                    "newText": "code",
                }
            ]
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

    async def test_reference_and_implementation(self) -> None:
        client = LspClient()
        fake_rpc = _FakeRpc()
        client._rpc = fake_rpc  # type: ignore[attr-defined]
        client._process = _DummyProcess()  # type: ignore[attr-defined]
        path = Path("demo.py")

        references = await client.references(path, 0, 0, include_declaration=False)
        implementations = await client.implementation(path, 0, 0)

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0][1:], (4, 5))
        self.assertEqual(len(implementations), 1)
        self.assertEqual(implementations[0][0].name, "impl.py")
        self.assertEqual(implementations[0][1:], (9, 3))

    async def test_symbols_queries(self) -> None:
        client = LspClient()
        fake_rpc = _FakeRpc()
        client._rpc = fake_rpc  # type: ignore[attr-defined]
        client._process = _DummyProcess()  # type: ignore[attr-defined]
        path = Path("demo.py")

        document_symbols = await client.document_symbols(path)
        workspace_symbols = await client.workspace_symbols("global")

        self.assertEqual(len(document_symbols), 2)
        self.assertIn("Outer", document_symbols[0][0])
        self.assertIn("Outer.inner", document_symbols[1][0])
        self.assertEqual(len(workspace_symbols), 1)
        self.assertIn("pkg.GlobalThing", workspace_symbols[0][0])

    async def test_rename_and_formatting(self) -> None:
        client = LspClient()
        fake_rpc = _FakeRpc()
        client._rpc = fake_rpc  # type: ignore[attr-defined]
        client._process = _DummyProcess()  # type: ignore[attr-defined]
        path = Path("demo.py")

        rename_payload = await client.rename(path, 0, 0, "renamed")
        edits = await client.formatting(path, tab_size=4, insert_spaces=True)

        self.assertTrue("changes" in rename_payload)
        self.assertEqual(len(edits), 1)


if __name__ == "__main__":
    unittest.main()
