# Touchpad

Use your phone as a wireless touchpad (and keyboard) for your computer. You don't install any app on the phone; it runs in the phone's browser.

## Setup (on your computer)

```bash
pip install -r requirements.txt
python server.py
```

The server prints a URL and a QR code. Scan the code with your phone or open the URL. **Both devices must be on the same Wi-Fi.**

## Gestures

| Gesture | Action |
|---|---|
| 1 finger drag | Move pointer |
| Tap | Left click |
| 2-finger tap | Right click |
| 3-finger tap | Middle click |
| 2-finger drag | Scroll |
| Drag on the right-edge strip | Scroll (1 finger) |
| Double-tap & hold, then drag | Click-and-drag |
| Left / Middle / Right buttons | Press & hold mouse buttons |
| ⌨︎ button | Type on the computer with the phone keyboard |
| ⚙︎ button | Speed, acceleration, scroll speed, natural scrolling, scroll momentum |

## Options

```
python server.py --port 8765 --token mysecret   # fixed token instead of random
python server.py --dry-run                      # print events, don't move the mouse
```

## Notes

- **Security:** a random token in the URL protects the server, and it changes on each start unless you pass `--token`. Use it only on trusted networks, because traffic is unencrypted HTTP.
- **macOS:** allow your terminal under *System Settings → Privacy & Security → Accessibility*, or the mouse won't move.
- **Linux:** needs an X11 session. On Wayland, pynput can't move the pointer.
- **Firewall:** allow incoming connections on the port (default 8765) if the phone can't connect.

## Also in this repo

- [`netbond/`](netbond/README.md) lets a Mac use several routers / internet lines at once, with load balancing and automatic failover.
