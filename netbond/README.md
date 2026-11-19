# netbond

netbond lets one Mac use several routers, each with its own internet line, at the same time. It runs a small proxy on your Mac. Each new connection goes out through whichever line is least busy. If a line loses internet, netbond skips it until it comes back.

It needs Python 3.9+ only, with nothing to install.

## 1. Connect the Mac to every router

Each router needs its own connection to the Mac, for example:

| Router | How the Mac connects |
|---|---|
| Router A | Wi-Fi |
| Router B | Ethernet cable, or a USB/Thunderbolt Ethernet adapter |
| Router C | Another USB Ethernet adapter, or a phone's USB tethering |

A Mac has only one Wi-Fi radio, so it can join only one router by Wi-Fi. Use cables or adapters for the others.

In **System Settings → Network**, every connection should show a green "Connected" dot. They can stay in any order.

## 2. Check what netbond sees

```bash
cd netbond
python3 netbond.py list
```

Example:

```
Interface  Local IP         Router           Internet   Latency   Public IP
en0        192.168.1.23     192.168.1.1      OK         18 ms     203.0.113.10
en7        192.168.0.40     192.168.0.1      OK         25 ms     198.51.100.7

Ready: 2 separate internet connections. Run:  python3 netbond.py start
```

Different public IPs mean the lines really are separate. If two lines show the same public IP, they share one internet line, and combining them won't add speed.

## 3. Start it

```bash
sudo python3 netbond.py start --system-proxy
```

`--system-proxy` points macOS at netbond, so Safari, Chrome, Mail, the App Store and most other apps use every line. Changing the system proxy needs `sudo`. When you press **Ctrl+C**, netbond turns the system proxy back off.

You'll see a live report:

```
[14:02:11] en0[up] 4.1 MB/s, 6 open | en7[up] 3.8 MB/s, 5 open  => total 7.9 MB/s
```

### If the Mac crashes or loses power while netbond runs

The system proxy stays set and web pages won't load. Turn it off in **System Settings → Network → (your connection) → Details → Proxies**, or run:

```bash
sudo python3 -c "import netbond; netbond.set_system_proxy(1080, False)"
```

### Without sudo

Run `python3 netbond.py start`, then set the proxy yourself in **System Settings → Network → (connection) → Details → Proxies**. Turn on *Web proxy*, *Secure web proxy* and *SOCKS proxy*, each set to `127.0.0.1` port `1080`.

## Options

```bash
python3 netbond.py start -i en0,en7          # use only these interfaces
python3 netbond.py start -w en0=3,en7=1      # en0 is ~3x faster, so give it 3x the connections
python3 netbond.py start --sticky 600        # keep each website on one line for 10 min
python3 netbond.py start --host 0.0.0.0      # let other devices on your LAN use this Mac as their proxy
python3 netbond.py start -p 8888 -q          # other port, no speed reports
```

- **Weights:** use these when your lines have very different speeds. For example, set weights to `en0=4,en7=1` for 100 Mbps + 25 Mbps.
- **Sticky:** use this if a bank, streaming service or other site logs you out. Those sites can dislike your IP address changing between requests.
- **`--host 0.0.0.0`:** anyone on your network can then use the proxy, so only do this on a network you trust.

## What to expect

netbond balances **connections**, not individual packets.

- **Gets faster:** loading many things at once, such as busy web pages, several downloads, a browser plus a game update, or download managers and torrent clients that open many connections.
- **Doesn't get faster:** one single connection, such as one plain file download or one video stream. Each of these is limited to the line it lands on. Truly splitting one connection across two internet lines needs a server on the internet that puts the pieces back together. Services like Speedify do this, and no tool that runs only on your Mac can.
- **Not covered:** apps that ignore the system proxy, and UDP traffic, such as many games, video calls and some VPNs. These keep using macOS's main connection, the one at the top of the network list.
