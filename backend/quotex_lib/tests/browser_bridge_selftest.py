"""Self-test for the Chrome WebSocket bridge (api_quotex/browser_ws.py). No Quotex account or internet needed.

A fake Engine.IO v3 / Socket.IO server speaks the same frames as Quotex (text frames, a `451-[...]` placeholder
followed by a binary `\\x04<json>` frame). The real AsyncWebSocketClient connects to it through a real Chrome and
the script checks the handshake, text + binary events in order, sending, and close detection.

    python tests/browser_bridge_selftest.py            # needs Chrome; headless is fine here (no Cloudflare)
"""

import asyncio
import json
import os
import sys

os.environ["QUOTEX_TRANSPORT"] = "browser"
os.environ.setdefault("QUOTEX_BROWSER_HEADLESS", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import websockets  # noqa: E402

from api_quotex.websocket_client import AsyncWebSocketClient  # noqa: E402

PORT = 8766
os.environ["QUOTEX_PAGE_URL"] = f"http://127.0.0.1:{PORT}/"   # same-origin start page: recent Chrome blocks about:blank -> localhost sockets
INSTRUMENTS = [[36, "AUDCAD", "AUD/CAD", "currency", 5, 80] + [0] * 8 + [True], [66, "EURUSD_otc", "EUR/USD (OTC)", "currency", 5, 91] + [0] * 8 + [True]]
TICKS = 40


async def serve_page(path, request_headers):
    """Plain HTTP request (no WebSocket upgrade): answer with a tiny page so Chrome has a real origin to start from."""
    if request_headers.get("Upgrade", "").lower() != "websocket":
        return 200, [("Content-Type", "text/html")], b"<html><body>fake quotex</body></html>"
    return None


async def fake_quotex(ws):
    await ws.send('0{"sid":"abc","upgrades":[],"pingInterval":25000,"pingTimeout":5000}')
    ticks_task = None
    async for msg in ws:
        if msg == "40":
            await ws.send("40")
        elif msg == "2":
            await ws.send("3")
        elif isinstance(msg, str) and msg.startswith('42["authorization"'):
            await ws.send('42["s_authorization",{}]')
            await ws.send('451-["instruments/list",{"_placeholder":true,"num":0}]')
            await ws.send(b"\x04" + json.dumps(INSTRUMENTS).encode())

            async def ticks():
                for i in range(TICKS):
                    await ws.send('451-["quotes/stream",{"_placeholder":true,"num":0}]')
                    await ws.send(b"\x04" + json.dumps([["EURUSD_otc", 1792934000.0 + i, 1.0 + i / 1000, 0]]).encode())
                    await asyncio.sleep(0.02)
                await asyncio.sleep(1.5)
                await ws.close()          # server hangs up: the client must notice
            ticks_task = asyncio.create_task(ticks())
        elif isinstance(msg, str) and msg.startswith('42["echo"'):
            await ws.send('42["echo_reply",' + msg[len('42["echo",'):])
    if ticks_task:
        ticks_task.cancel()


async def main() -> int:
    seen = {"authenticated": 0, "assets": None, "quotes": [], "echo": None, "disconnected": 0}
    async with websockets.serve(fake_quotex, "127.0.0.1", PORT, process_request=serve_page):
        client = AsyncWebSocketClient()
        client.on("authenticated", lambda d: seen.__setitem__("authenticated", seen["authenticated"] + 1))
        client.on("assets_list", lambda d: seen.__setitem__("assets", d))
        client.on("quote_stream", lambda d: seen["quotes"].append(d[0][2]))
        client.on("json_data", lambda d: seen.__setitem__("echo", d) if d.get("event") == "echo_reply" else None)
        client.on("disconnected", lambda d: seen.__setitem__("disconnected", seen["disconnected"] + 1))

        print("connecting through Chrome ...")
        ok = await client.connect([f"ws://127.0.0.1:{PORT}/socket.io/?EIO=3&transport=websocket"],
                                  '42["authorization",{"session":"test","isDemo":1,"tournamentId":0}]')
        print("handshake:", "ok" if ok else "FAILED")
        await asyncio.sleep(0.8)
        await client.send_message('42["echo",{"n":7}]')
        await asyncio.sleep(2.5)
        await client.disconnect()

    from api_quotex.browser_ws import get_session
    await get_session().shutdown()

    checks = {
        "handshake succeeded": ok,
        "authenticated event": seen["authenticated"] >= 1,
        "binary instrument list decoded": isinstance(seen["assets"], list) and len(seen["assets"]) == 2,
        f"all {TICKS} binary ticks received": len(seen["quotes"]) == TICKS,
        "ticks arrived in order": seen["quotes"] == sorted(seen["quotes"]),
        "message sent to server and answered": bool(seen["echo"]) and seen["echo"]["data"] == {"n": 7},
        "server hang-up detected": seen["disconnected"] >= 1,
    }
    for name, passed in checks.items():
        print(("PASS  " if passed else "FAIL  ") + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
