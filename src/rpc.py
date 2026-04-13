from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable


NotificationHandler = Callable[[Any], Any]
RequestHandler = Callable[[Any], Any]


class JsonRpcPeer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self._recv_task is None:
            self._recv_task = asyncio.create_task(self._recv_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("RPC peer closed."))
        self._pending.clear()

        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._recv_task = None

        try:
            self._writer.close()
            if hasattr(self._writer, "wait_closed"):
                await self._writer.wait_closed()
        except Exception:
            pass

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def send_request(self, method: str, params: Any) -> asyncio.Future[Any]:
        _request_id, future = self.send_request_with_id(method, params)
        return future

    def send_request_with_id(self, method: str, params: Any) -> tuple[int, asyncio.Future[Any]]:
        if self._closed:
            raise RuntimeError("RPC peer is closed.")
        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        asyncio.create_task(self._safe_send_request(payload, request_id))
        return request_id, future

    async def request(self, method: str, params: Any) -> Any:
        return await self.send_request(method, params)

    async def send_notification(self, method: str, params: Any) -> None:
        if self._closed:
            raise RuntimeError("RPC peer is closed.")
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send_payload(payload)

    async def _safe_send_request(self, payload: dict[str, Any], request_id: int) -> None:
        try:
            await self._send_payload(payload)
        except Exception as exc:
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_exception(RuntimeError(f"RPC write failed: {exc}"))

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        async with self._write_lock:
            self._writer.write(header + body)
            await self._writer.drain()

    async def _recv_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    message = await self._read_message(self._reader)
                except ValueError:
                    continue
                if message is None:
                    break
                if "id" in message and "method" in message:
                    await self._handle_request(message)
                    continue
                if "id" in message:
                    self._handle_response(message)
                    continue
                if "method" in message:
                    await self._handle_notification(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("RPC connection closed."))
            self._pending.clear()

    async def _handle_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return
        if not isinstance(request_id, (int, str)):
            return
        handler = self._request_handlers.get(method)
        if handler is None:
            await self._send_payload(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
            return
        try:
            result = handler(message.get("params"))
            if inspect.isawaitable(result):
                result = await result
            await self._send_payload(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            )
        except Exception as exc:
            await self._send_payload(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            )

    def _handle_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if message.get("error") is not None:
            future.set_exception(RuntimeError(str(message.get("error"))))
            return
        future.set_result(message.get("result"))

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        handlers = self._notification_handlers.get(method, [])
        for handler in handlers:
            try:
                outcome = handler(message.get("params"))
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                continue

    @staticmethod
    async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("ascii", errors="ignore").strip()
            if not decoded:
                break
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length_text = headers.get("content-length")
        if length_text is None:
            raise ValueError("Missing Content-Length header.")
        try:
            content_length = int(length_text)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if content_length < 0:
            raise ValueError("Negative Content-Length is invalid.")

        payload = await reader.readexactly(content_length)
        try:
            decoded = payload.decode("utf-8")
            message = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON-RPC payload.") from exc
        if not isinstance(message, dict):
            raise ValueError("JSON-RPC payload must be an object.")
        return message
