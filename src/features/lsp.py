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


class LspClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._rpc: JsonRpcPeer | None = None
        self._documents: dict[str, int] = {}
        self._diagnostics: dict[str, list[str]] = {}
        self._diagnostics_payload: dict[str, list[dict[str, Any]]] = {}
        self._completion_request_id: int | None = None
        self._command: tuple[str, ...] = ()
        self._root: Path | None = None

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

        await self.stop()
        self._command = clean
        self._root = normalized_root
        self._documents = {}
        self._diagnostics = {}
        self._diagnostics_payload = {}
        self._completion_request_id = None

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
                        "completion": {"completionItem": {"snippetSupport": False}},
                    }
                },
                "clientInfo": {"name": "pvim", "version": "0.2"},
            },
        )
        await self._notify("initialized", {})

    async def stop(self) -> None:
        self._documents = {}
        self._diagnostics = {}
        self._diagnostics_payload = {}
        self._completion_request_id = None

        if self._rpc is not None:
            try:
                await self._request("shutdown", {})
            except Exception:
                pass
            try:
                await self._notify("exit", {})
            except Exception:
                pass
            try:
                await self._rpc.close()
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
        rpc = self._rpc
        if rpc is None:
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
        rpc = self._rpc
        if rpc is None:
            raise RuntimeError("LSP transport is closed.")
        return await rpc.request(method, params)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        rpc = self._rpc
        if rpc is None:
            raise RuntimeError("LSP transport is closed.")
        await rpc.send_notification(method, params)

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
            line = int(start.get("line", 0)) + 1
            col = int(start.get("character", 0)) + 1
            resolved = _uri_to_path(uri)
            if resolved is None:
                continue
            out.append((resolved, max(1, line), max(1, col)))
        return out
