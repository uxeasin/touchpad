#!/usr/bin/env python3
"""Phone-as-touchpad server.

Run this on your computer, then open the printed URL on your phone
(both devices must be on the same Wi-Fi network).
"""

import argparse
import json
import secrets
import socket
from pathlib import Path

from aiohttp import WSMsgType, web

STATIC = Path(__file__).parent / "static"


def lan_ip() -> str:
    """Best-effort guess of this machine's LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Controller:
    """Translates touchpad messages into real mouse/keyboard actions."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        if dry_run:
            return
        from pynput.keyboard import Controller as Keyboard, Key
        from pynput.mouse import Button, Controller as Mouse

        self.mouse = Mouse()
        self.keyboard = Keyboard()
        self.Button = Button
        self.Key = Key
        self.buttons = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def handle(self, msg: dict) -> None:
        t = msg.get("t")
        if self.dry_run:
            print("event:", msg)
            return
        if t == "move":
            self.mouse.move(float(msg["dx"]), float(msg["dy"]))
        elif t == "click":
            btn = self.buttons.get(msg.get("b", "left"), self.Button.left)
            self.mouse.click(btn, int(msg.get("n", 1)))
        elif t == "down":
            self.mouse.press(self.buttons.get(msg.get("b", "left"), self.Button.left))
        elif t == "up":
            self.mouse.release(self.buttons.get(msg.get("b", "left"), self.Button.left))
        elif t == "scroll":
            # pynput: +dy scrolls up, +dx scrolls right (direction is decided on the phone)
            self.mouse.scroll(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
        elif t == "text":
            self.keyboard.type(str(msg.get("s", "")))
        elif t == "key":
            key = getattr(self.Key, str(msg.get("k", "")), None)
            if key is not None:
                self.keyboard.tap(key)


def make_app(token: str, controller: Controller) -> web.Application:
    async def index(request: web.Request) -> web.StreamResponse:
        if request.query.get("token") != token:
            return web.Response(status=403, text="Invalid or missing token. Use the URL printed by the server.")
        return web.FileResponse(STATIC / "index.html")

    async def ws(request: web.Request) -> web.StreamResponse:
        if request.query.get("token") != token:
            raise web.HTTPForbidden()
        sock = web.WebSocketResponse(heartbeat=20)
        await sock.prepare(request)
        print(f"Phone connected from {request.remote}")
        async for m in sock:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                controller.handle(json.loads(m.data))
            except Exception as e:  # keep the session alive on bad input
                print("bad message:", m.data, e)
        print(f"Phone disconnected ({request.remote})")
        return sock

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws)
    return app


def print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--token", help="access token (random if omitted)")
    p.add_argument("--dry-run", action="store_true", help="print events instead of moving the mouse")
    args = p.parse_args()

    token = args.token or secrets.token_urlsafe(8)
    url = f"http://{lan_ip()}:{args.port}/?token={token}"
    print("\nOpen this on your phone (same Wi-Fi):\n")
    print(f"  {url}\n")
    print_qr(url)
    web.run_app(make_app(token, Controller(args.dry_run)), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
