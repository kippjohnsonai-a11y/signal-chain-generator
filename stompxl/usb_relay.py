"""The browser side of a relayed pedal: a WebSocket that carries USB calls.

When the pedal is plugged into a phone or laptop instead of the Mac running
the app, the page there opens the pedal with WebUSB and connects to
``/api/usb-relay``. The pedal code on the server keeps doing everything it
always did, through :mod:`helixgen.device.relay`; each bulk read or write it
makes becomes one JSON message to the page and one answer back.

Pedal work runs on worker threads (jobs, sync endpoints) while the WebSocket
lives on the event loop, so :meth:`WebSocketTransport.call` hands each request
to the loop and blocks the worker until the page answers.

Messages, server to page::

    {"id": 7, "op": "write", "ep": 1, "data": "<base64>", "timeout": 2000}
    {"id": 8, "op": "read", "ep": 1, "size": 512, "timeout": 2000}
    {"id": 9, "op": "clear_halt", "ep": 1, "direction": "in"}

and back::

    {"id": 7, "ok": true, "written": 12}
    {"id": 8, "ok": true, "data": "<base64>"}
    {"id": 8, "ok": false, "error": "timeout", "timeout": true}

The page's first message describes the pedal it opened::

    {"type": "hello", "vendorId": 3649, "productId": 16979,
     "productName": "HX Stomp XL", "serialNumber": "...", "where": "Android phone"}
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from stompxl.helixgen.device import relay
from stompxl.helixgen.device.usb import PRODUCT_IDS, VENDOR_ID, DeviceInfo

logger = logging.getLogger(__name__)


class WebSocketTransport:
    """One connected page. Safe to call from any thread but the event loop's."""

    def __init__(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop):
        self._ws = websocket
        self._loop = loop
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False
        #: The pedal's OUT packet size as the page's browser sees it.
        self.packet_size: int | None = None
        #: One USB call in flight at a time, matching the device's strict
        #: request/response rhythm.
        self._send_lock = asyncio.Lock()

    def call(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        if self._closed:
            raise relay.RelayError("The device holding the pedal disconnected.")
        future = asyncio.run_coroutine_threadsafe(self._call(request), self._loop)
        try:
            reply = future.result(timeout=timeout_s)
        except TimeoutError as exc:
            future.cancel()
            raise relay.RelayError("The device holding the pedal stopped answering.") from exc
        if "data" in reply and isinstance(reply["data"], str):
            reply["data"] = base64.b64decode(reply["data"])
        return reply

    async def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._send_lock:
            message = dict(request)
            if isinstance(message.get("data"), bytes | bytearray):
                message["data"] = base64.b64encode(message["data"]).decode("ascii")
            message["id"] = next(self._ids)
            future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
            self._pending[message["id"]] = future
            try:
                await self._ws.send_json(message)
                return await future
            finally:
                self._pending.pop(message["id"], None)

    def answer(self, message: dict[str, Any]) -> None:
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is not None and not future.done():
            future.set_result(message)

    def close(self) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_result({"ok": False, "error": "The device holding the pedal disconnected."})


def device_info(hello: dict[str, Any]) -> DeviceInfo:
    vendor = int(hello.get("vendorId") or VENDOR_ID)
    product = int(hello.get("productId") or 0)
    return DeviceInfo(
        vendor_id=vendor,
        product_id=product,
        product_name=PRODUCT_IDS.get(product, hello.get("productName") or f"device 0x{product:04x}"),
        serial_number=hello.get("serialNumber") or None,
        bus=None,
        address=None,
    )


async def serve(websocket: WebSocket) -> None:
    """Run one relay connection until the page goes away."""
    await websocket.accept()
    try:
        hello = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    if hello.get("type") != "hello" or int(hello.get("vendorId") or 0) != VENDOR_ID:
        await websocket.close(code=1008, reason="Expected a hello naming a Line 6 device")
        return

    transport = WebSocketTransport(websocket, asyncio.get_running_loop())
    # 64 when the pedal came up at full speed there; a page from before this
    # doesn't say, and is taken to be the usual 512.
    transport.packet_size = int(hello.get("packetSize") or 0) or None
    info = device_info(hello)
    where = str(hello.get("where") or "another device")[:60]
    relay.attach(info, transport, where)
    logger.info("Pedal relayed from %s: %s", where, info.describe())
    await websocket.send_json({"type": "attached", "description": info.describe()})
    try:
        while True:
            message = await websocket.receive_json()
            if "id" in message:
                transport.answer(message)
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        transport.close()
        relay.detach(transport)
        logger.info("Relay from %s closed", where)
