from __future__ import annotations

import asyncio
import unittest

from src.rpc import JsonRpcPeer


class JsonRpcPeerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._server_peer_ready: asyncio.Future[JsonRpcPeer] = asyncio.get_running_loop().create_future()

        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            peer = JsonRpcPeer(reader, writer)
            await peer.start()
            self._server_peer_ready.set_result(peer)

        self._server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        address = self._server.sockets[0].getsockname()
        client_reader, client_writer = await asyncio.open_connection(address[0], address[1])
        self.client_peer = JsonRpcPeer(client_reader, client_writer)
        await self.client_peer.start()
        self.server_peer = await asyncio.wait_for(self._server_peer_ready, timeout=1.0)

    async def asyncTearDown(self) -> None:
        await self.client_peer.close()
        await self.server_peer.close()
        self._server.close()
        await self._server.wait_closed()

    async def test_high_concurrency_requests(self) -> None:
        self.server_peer.on_request("echo", lambda params: params)
        futures = [self.client_peer.send_request("echo", {"id": index}) for index in range(1000)]
        results = await asyncio.gather(*futures)
        self.assertEqual(len(results), 1000)
        self.assertEqual(results[0], {"id": 0})
        self.assertEqual(results[-1], {"id": 999})

    async def test_notifications_dispatch(self) -> None:
        seen: list[str] = []
        done = asyncio.Event()

        def _on_log(params: object) -> None:
            if isinstance(params, dict) and isinstance(params.get("message"), str):
                seen.append(params["message"])
                done.set()

        self.server_peer.on_notification("log", _on_log)
        await self.client_peer.send_notification("log", {"message": "hello"})
        await asyncio.wait_for(done.wait(), timeout=1.0)
        self.assertEqual(seen, ["hello"])

    async def test_invalid_json_payload_raises_clear_error(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 5\r\n\r\n{bad}")
        reader.feed_eof()
        with self.assertRaises(ValueError):
            await JsonRpcPeer._read_message(reader)


if __name__ == "__main__":
    unittest.main()
