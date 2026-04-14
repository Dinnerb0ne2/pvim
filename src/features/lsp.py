from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..rpc import JsonRpcPeer


def _path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _uri_to_path(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


def _extract_hover_text(contents: Any) -> str:
    if isinstance(contents, str):
        return contents.strip()
    if isinstance(contents, dict):
        if isinstance(contents.get("value"), str):
            return contents["value"].strip()
        if isinstance(contents.get("language"), str) and isinstance(contents.get("value"), str):
            return contents["value"].strip()
        return ""
    if isinstance(contents, list):
        items = [_extract_hover_text(item) for item in contents]
        return "\n\n".join(item for item in items if item)
    return ""


def _extract_completion_items(payload: Any) -> list[str]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("insertText") or item.get("label")
        if not isinstance(label, str):
            continue
        clean = label.strip()
        if len(clean) < 1 or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _symbol_kind_name(kind: Any) -> str:
    names = {
        1: "File",
        2: "Module",
        3: "Namespace",
        4: "Package",
        5: "Class",
        6: "Method",
        7: "Property",
        8: "Field",
        9: "Constructor",
        10: "Enum",
        11: "Interface",
        12: "Function",
        13: "Variable",
        14: "Constant",
        15: "String",
        16: "Number",
        17: "Boolean",
        18: "Array",
        19: "Object",
        20: "Key",
        21: "Null",
        22: "EnumMember",
        23: "Struct",
        24: "Event",
        25: "Operator",
        26: "TypeParameter",
    }
    try:
        return names.get(int(kind), "Symbol")
    except (TypeError, ValueError):
        return "Symbol"


class LspClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._rpc: JsonRpcPeer | None = None
        self._documents: dict[str, int] = {}
        self._document_states: dict[str, tuple[Path, str, str]] = {}
        self._diagnostics: dict[str, list[str]] = {}
        self._diagnostics_payload: dict[str, list[dict[str, Any]]] = {}
        self._completion_request_id: int | None = None
        self._command: tuple[str, ...] = ()
        self._root: Path | None = None
        self._request_queue: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future[Any]]] | None = None
        self._request_worker: asyncio.Task[None] | None = None
        self._restart_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def ensure_started(self, command: list[str], root: Path) -> None:
        clean = tuple(item for item in command if item.strip())
        if not clean:
            raise RuntimeError("LSP command is empty.")
        normalized_root = root.resolve()
        if self.running and self._command == clean and self._root == normalized_root:
            return

        same_target = self._command == clean and self._root == normalized_root
        saved_states = dict(self._document_states) if same_target else {}
        await self.stop(clear_documents=not same_target)
        self._command = clean
        self._root = normalized_root
        self._documents = {}
        self._diagnostics = {}
        self._diagnostics_payload = {}
        self._completion_request_id = None
        if not same_target:
            self._document_states = {}

        process = await asyncio.create_subprocess_exec(
            *clean,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(normalized_root),
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise RuntimeError("LSP process stdio is unavailable.")

        self._process = process
        self._rpc = JsonRpcPeer(process.stdout, process.stdin)
        self._rpc.on_notification("textDocument/publishDiagnostics", self._on_publish_diagnostics)
        await self._rpc.start()
        self._request_queue = asyncio.Queue()
        self._request_worker = asyncio.create_task(self._request_loop())

        root_uri = _path_to_uri(normalized_root)
        await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": normalized_root.name or str(normalized_root)}],
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "implementation": {"linkSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "rename": {"prepareSupport": False},
                        "formatting": {},
                        "codeAction": {},
                        "completion": {"completionItem": {"snippetSupport": False}},
                    },
                    "workspace": {
                        "symbol": {},
                    },
                },
                "clientInfo": {"name": "pvim", "version": "0.6"},
            },
        )
        await self._notify("initialized", {})
        if saved_states:
            await self._restore_document_states(saved_states)

    async def stop(self, *, clear_documents: bool = True) -> None:
        self._documents = {}
        self._diagnostics = {}
        self._diagnostics_payload = {}
        self._completion_request_id = None
        if clear_documents:
            self._document_states = {}

        queue = self._request_queue
        self._request_queue = None
        if queue is not None:
            while not queue.empty():
                try:
                    _method, _params, future = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not future.done():
                    future.set_exception(RuntimeError("LSP request queue closed."))
                queue.task_done()
        if self._request_worker is not None:
            self._request_worker.cancel()
            try:
                await self._request_worker
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._request_worker = None

        if self._rpc is not None:
            rpc = self._rpc
            try:
                await rpc.request("shutdown", {})
            except Exception:
                pass
            try:
                await rpc.send_notification("exit", {})
            except Exception:
                pass
            try:
                await rpc.close()
            except Exception:
                pass
            self._rpc = None

        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._process = None
        self._command = ()
        self._root = None

    async def _ensure_transport(self) -> JsonRpcPeer:
        if self.running and self._rpc is not None:
            return self._rpc
        if not self._command or self._root is None:
            raise RuntimeError("LSP transport is closed.")
        await self.ensure_started(list(self._command), self._root)
        if self._rpc is None:
            raise RuntimeError("LSP transport is unavailable.")
        return self._rpc

    async def _request_loop(self) -> None:
        queue = self._request_queue
        if queue is None:
            return
        while True:
            method, params, future = await queue.get()
            try:
                if future.cancelled():
                    continue
                result = await self._request_direct(method, params)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_exception(RuntimeError("LSP request cancelled."))
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                queue.task_done()

    async def _request_direct(self, method: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            rpc = await self._ensure_transport()
            try:
                return await rpc.request(method, params)
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self._command and self._root is not None:
                    await self._restart_transport()
                    continue
                break
        if last_error is None:
            raise RuntimeError("LSP request failed.")
        raise last_error

    async def _restart_transport(self) -> None:
        async with self._restart_lock:
            if not self._command or self._root is None:
                raise RuntimeError("LSP restart target is missing.")
            saved_states = dict(self._document_states)
            command = list(self._command)
            root = self._root
            await self.stop(clear_documents=False)
            self._command = ()
            self._root = None
            await self.ensure_started(command, root)
            if saved_states:
                await self._restore_document_states(saved_states)

    async def _restore_document_states(self, states: dict[str, tuple[Path, str, str]]) -> None:
        for uri, payload in states.items():
            path, language_id, text = payload
            if not isinstance(uri, str) or not uri:
                continue
            try:
                await self.sync_document(path, text.split("\n"), language_id)
            except Exception:
                continue

    async def sync_document(self, path: Path, lines: list[str], language_id: str) -> None:
        if not self.running:
            raise RuntimeError("LSP is not running.")
        uri = _path_to_uri(path)
        text = "\n".join(lines)
        version = self._documents.get(uri, 0) + 1

        if uri not in self._documents:
            await self._notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": text,
                    }
                },
            )
        else:
            await self._notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        self._documents[uri] = version
        self._document_states[uri] = (path.resolve(), language_id, text)

    async def definition(self, path: Path, line: int, column: int) -> list[tuple[Path, int, int]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
            },
        )
        return self._extract_locations(payload)

    async def references(
        self,
        path: Path,
        line: int,
        column: int,
        *,
        include_declaration: bool = True,
    ) -> list[tuple[Path, int, int]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
                "context": {"includeDeclaration": bool(include_declaration)},
            },
        )
        return self._extract_locations(payload)

    async def implementation(self, path: Path, line: int, column: int) -> list[tuple[Path, int, int]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/implementation",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
            },
        )
        return self._extract_locations(payload)

    async def hover(self, path: Path, line: int, column: int) -> str:
        if not self.running:
            return ""
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
            },
        )
        if not isinstance(payload, dict):
            return ""
        return _extract_hover_text(payload.get("contents"))

    async def completion(self, path: Path, line: int, column: int) -> list[str]:
        if not self.running:
            return []
        try:
            rpc = await self._ensure_transport()
        except Exception:
            return []
        uri = _path_to_uri(path)
        if self._completion_request_id is not None:
            await rpc.send_notification("$/cancelRequest", {"id": self._completion_request_id})
        request_id, future = rpc.send_request_with_id(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
                "context": {"triggerKind": 1},
            },
        )
        self._completion_request_id = request_id
        payload: Any = None
        try:
            payload = await future
        except Exception:
            payload = None
        finally:
            if self._completion_request_id == request_id:
                self._completion_request_id = None
        return _extract_completion_items(payload)

    async def rename(self, path: Path, line: int, column: int, new_name: str) -> dict[str, Any]:
        if not self.running:
            return {}
        clean = new_name.strip()
        if not clean:
            return {}
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
                "newName": clean,
            },
        )
        if isinstance(payload, dict):
            return payload
        return {}

    async def formatting(self, path: Path, *, tab_size: int, insert_spaces: bool) -> list[dict[str, Any]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/formatting",
            {
                "textDocument": {"uri": uri},
                "options": {
                    "tabSize": max(1, int(tab_size)),
                    "insertSpaces": bool(insert_spaces),
                    "trimTrailingWhitespace": True,
                    "insertFinalNewline": False,
                    "trimFinalNewlines": False,
                },
            },
        )
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def document_symbols(self, path: Path) -> list[tuple[str, Path, int, int]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/documentSymbol",
            {
                "textDocument": {"uri": uri},
            },
        )
        return self._extract_symbols(payload, default_path=path.resolve())

    async def workspace_symbols(self, query: str) -> list[tuple[str, Path, int, int]]:
        if not self.running:
            return []
        clean = query.strip()
        if not clean:
            return []
        payload = await self._request(
            "workspace/symbol",
            {
                "query": clean,
            },
        )
        return self._extract_symbols(payload, default_path=None)

    async def diagnostics(self, path: Path) -> list[str]:
        uri = _path_to_uri(path)
        return list(self._diagnostics.get(uri, []))

    async def diagnostics_raw(self, path: Path) -> list[dict[str, Any]]:
        uri = _path_to_uri(path)
        return [dict(item) for item in self._diagnostics_payload.get(uri, [])]

    async def code_actions(
        self,
        path: Path,
        line: int,
        column: int,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.running:
            return []
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": {
                    "start": {"line": max(0, line), "character": max(0, column)},
                    "end": {"line": max(0, line), "character": max(0, column)},
                },
                "context": {"diagnostics": diagnostics if diagnostics is not None else []},
            },
        )
        if not isinstance(payload, list):
            return []
        actions: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                actions.append(item)
        return actions

    async def execute_command(self, command: str, arguments: list[Any] | None = None) -> Any:
        if not self.running:
            raise RuntimeError("LSP is not running.")
        return await self._request(
            "workspace/executeCommand",
            {
                "command": command,
                "arguments": arguments or [],
            },
        )

    async def _on_publish_diagnostics(self, params: Any) -> None:
        self._consume_diagnostics(params)

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        queue = self._request_queue
        if queue is None:
            return await self._request_direct(method, params)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await queue.put((method, params, future))
        return await future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        last_error: Exception | None = None
        for attempt in range(2):
            rpc = await self._ensure_transport()
            try:
                await rpc.send_notification(method, params)
                return
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self._command and self._root is not None:
                    await self._restart_transport()
                    continue
                break
        if last_error is None:
            raise RuntimeError("LSP notify failed.")
        raise last_error

    def _consume_diagnostics(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        payload = params.get("diagnostics", [])
        if not isinstance(payload, list):
            self._diagnostics[uri] = []
            self._diagnostics_payload[uri] = []
            return
        self._diagnostics_payload[uri] = [item for item in payload if isinstance(item, dict)]

        severity_map = {
            1: "Error",
            2: "Warning",
            3: "Info",
            4: "Hint",
        }
        lines: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message", "")).strip()
            if not message:
                continue

            line = 1
            col = 1
            range_obj = item.get("range")
            if isinstance(range_obj, dict):
                start = range_obj.get("start")
                if isinstance(start, dict):
                    try:
                        line = int(start.get("line", 0)) + 1
                    except (TypeError, ValueError):
                        line = 1
                    try:
                        col = int(start.get("character", 0)) + 1
                    except (TypeError, ValueError):
                        col = 1
            line = max(1, line)
            col = max(1, col)

            severity_value = item.get("severity", 0)
            try:
                severity = severity_map.get(int(severity_value), "Note")
            except (TypeError, ValueError):
                severity = "Note"

            source = item.get("source")
            code = item.get("code")
            extra = ""
            if isinstance(source, str) and source.strip():
                extra += f" {source.strip()}"
            if code is not None:
                extra += f"[{code}]"
            lines.append(f"{severity} Ln {line}:{col}{extra} {message}")
        self._diagnostics[uri] = lines

    def _extract_locations(self, payload: Any) -> list[tuple[Path, int, int]]:
        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            return []

        out: list[tuple[Path, int, int]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            range_obj = item.get("range")
            if not isinstance(uri, str):
                uri = item.get("targetUri")
                range_obj = item.get("targetSelectionRange") or item.get("targetRange")
            if not isinstance(uri, str) or not isinstance(range_obj, dict):
                continue
            start = range_obj.get("start")
            if not isinstance(start, dict):
                continue
            try:
                line = int(start.get("line", 0)) + 1
            except (TypeError, ValueError):
                line = 1
            try:
                col = int(start.get("character", 0)) + 1
            except (TypeError, ValueError):
                col = 1
            resolved = _uri_to_path(uri)
            if resolved is None:
                continue
            out.append((resolved, max(1, line), max(1, col)))
        return out

    def _extract_symbols(self, payload: Any, *, default_path: Path | None) -> list[tuple[str, Path, int, int]]:
        if not isinstance(payload, list):
            return []
        out: list[tuple[str, Path, int, int]] = []

        def _walk_document_symbol(item: dict[str, Any], parent_name: str = "") -> None:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                return
            detail = item.get("detail")
            kind_text = _symbol_kind_name(item.get("kind"))
            cleaned_name = name.strip()
            label_name = f"{parent_name}.{cleaned_name}" if parent_name else cleaned_name
            if isinstance(detail, str) and detail.strip():
                label = f"{kind_text}: {label_name}  {detail.strip()}"
            else:
                label = f"{kind_text}: {label_name}"

            range_obj = item.get("selectionRange")
            if not isinstance(range_obj, dict):
                range_obj = item.get("range")
            location = self._range_to_line_col(range_obj)
            if location is not None and default_path is not None:
                line, col = location
                out.append((label, default_path, line, col))

            children = item.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        _walk_document_symbol(child, label_name)

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if "location" in entry:
                location_obj = entry.get("location")
                if not isinstance(location_obj, dict):
                    continue
                uri = location_obj.get("uri")
                range_obj = location_obj.get("range")
                if not isinstance(uri, str) or not isinstance(range_obj, dict):
                    continue
                resolved = _uri_to_path(uri)
                position = self._range_to_line_col(range_obj)
                if resolved is None or position is None:
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                kind_text = _symbol_kind_name(entry.get("kind"))
                container = entry.get("containerName")
                prefix = f"{container.strip()}." if isinstance(container, str) and container.strip() else ""
                label = f"{kind_text}: {prefix}{name.strip()}"
                out.append((label, resolved, position[0], position[1]))
                continue
            _walk_document_symbol(entry)
        return out

    def _range_to_line_col(self, range_obj: Any) -> tuple[int, int] | None:
        if not isinstance(range_obj, dict):
            return None
        start = range_obj.get("start")
        if not isinstance(start, dict):
            return None
        try:
            line = int(start.get("line", 0)) + 1
        except (TypeError, ValueError):
            line = 1
        try:
            col = int(start.get("character", 0)) + 1
        except (TypeError, ValueError):
            col = 1
        return max(1, line), max(1, col)
