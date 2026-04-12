from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


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
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._documents: dict[str, int] = {}
        self._diagnostics: dict[str, list[str]] = {}
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
        self._reader = process.stdout
        self._writer = process.stdin
        self._reader_task = asyncio.create_task(self._reader_loop())

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
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending = {}
        self._documents = {}
        self._diagnostics = {}

        if self._writer is not None:
            try:
                await self._request("shutdown", {})
            except Exception:
                pass
            try:
                await self._notify("exit", {})
            except Exception:
                pass

        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._reader_task = None

        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._process = None
        self._reader = None
        self._writer = None
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
        uri = _path_to_uri(path)
        payload = await self._request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, line), "character": max(0, column)},
                "context": {"triggerKind": 1},
            },
        )
        return _extract_completion_items(payload)

    async def diagnostics(self, path: Path) -> list[str]:
        uri = _path_to_uri(path)
        return list(self._diagnostics.get(uri, []))

    async def _reader_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                message = await self._read_message(reader)
                if message is None:
                    break
                method = message.get("method")
                if method == "textDocument/publishDiagnostics":
                    self._consume_diagnostics(message.get("params"))
                    continue
                if "id" in message:
                    request_id = message.get("id")
                    if not isinstance(request_id, int):
                        continue
                    future = self._pending.pop(request_id, None)
                    if future is None or future.done():
                        continue
                    if message.get("error") is not None:
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(f"LSP reader failed: {exc}"))
            self._pending = {}
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("LSP connection closed."))
            self._pending = {}

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        writer = self._writer
        if writer is None:
            raise RuntimeError("LSP transport is closed.")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return await future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            raise RuntimeError("LSP transport is closed.")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        writer.write(header + body)
        await writer.drain()

    async def _read_message(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                return None
            text = line.decode("ascii", errors="ignore").strip()
            if not text:
                break
            if ":" not in text:
                continue
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length_text = headers.get("content-length")
        if length_text is None:
            return None
        try:
            length = int(length_text)
        except ValueError:
            return None
        if length <= 0:
            return None

        raw = await reader.readexactly(length)
        decoded = raw.decode("utf-8", errors="ignore")
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            return None
        return payload

    def _consume_diagnostics(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        payload = params.get("diagnostics", [])
        if not isinstance(payload, list):
            self._diagnostics[uri] = []
            return

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
