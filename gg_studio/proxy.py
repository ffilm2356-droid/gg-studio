"""VPN-to-proxy bridge — expose local VPN as SOCKS5 proxy for accounts.

If you're running a VPN on the machine, this creates a local SOCKS5 proxy
that routes through the VPN interface, so each account can use a different
VPN connection without buying external proxies.

Usage:
  python -m gg_studio.proxy --interface tun0 --port 1080
  python -m gg_studio.proxy --list-interfaces
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import subprocess
import sys
from typing import Optional

logger = logging.getLogger("gg_studio.proxy")


def list_vpn_interfaces() -> list[dict]:
    """List available network interfaces that look like VPN tunnels."""
    interfaces = []
    try:
        import netifaces  # type: ignore
        for iface in netifaces.interfaces():
            if any(iface.startswith(p) for p in ("tun", "tap", "wg", "ppp", "utun")):
                addrs = netifaces.ifaddresses(iface)
                ipv4 = addrs.get(netifaces.AF_INET, [])
                if ipv4:
                    interfaces.append({
                        "name": iface,
                        "ip": ipv4[0].get("addr", ""),
                    })
    except ImportError:
        result = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            import json
            for iface in json.loads(result.stdout):
                name = iface.get("ifname", "")
                if any(name.startswith(p) for p in ("tun", "tap", "wg", "ppp")):
                    for info in iface.get("addr_info", []):
                        if info.get("family") == "inet":
                            interfaces.append({
                                "name": name,
                                "ip": info.get("local", ""),
                            })
    return interfaces


class LocalSocks5Proxy:
    """Minimal SOCKS5 proxy that binds to a specific network interface."""

    def __init__(
        self,
        bind_address: str = "127.0.0.1",
        port: int = 1080,
        source_address: Optional[str] = None,
    ):
        self.bind_address = bind_address
        self.port = port
        self.source_address = source_address
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client,
            self.bind_address,
            self.port,
        )
        logger.info(
            "SOCKS5 proxy listening on %s:%d (source: %s)",
            self.bind_address,
            self.port,
            self.source_address or "default",
        )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        try:
            header = await reader.readexactly(2)
            version, nmethods = struct.unpack("!BB", header)
            if version != 5:
                writer.close()
                return

            methods = await reader.readexactly(nmethods)
            writer.write(struct.pack("!BB", 5, 0))
            await writer.drain()

            header = await reader.readexactly(4)
            version, cmd, _, atyp = struct.unpack("!BBBB", header)

            if cmd != 1:
                writer.write(struct.pack("!BBBBIH", 5, 7, 0, 1, 0, 0))
                await writer.drain()
                writer.close()
                return

            if atyp == 1:
                addr_bytes = await reader.readexactly(4)
                dst_addr = socket.inet_ntoa(addr_bytes)
            elif atyp == 3:
                length = (await reader.readexactly(1))[0]
                dst_addr = (await reader.readexactly(length)).decode()
            elif atyp == 4:
                addr_bytes = await reader.readexactly(16)
                dst_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                writer.close()
                return

            dst_port = struct.unpack("!H", await reader.readexactly(2))[0]

            try:
                if self.source_address:
                    remote_reader, remote_writer = await asyncio.open_connection(
                        dst_addr,
                        dst_port,
                        local_addr=(self.source_address, 0),
                    )
                else:
                    remote_reader, remote_writer = await asyncio.open_connection(
                        dst_addr, dst_port
                    )
            except Exception:
                writer.write(struct.pack("!BBBBIH", 5, 5, 0, 1, 0, 0))
                await writer.drain()
                writer.close()
                return

            writer.write(struct.pack("!BBBBIH", 5, 0, 0, 1, 0, 0))
            await writer.drain()

            await asyncio.gather(
                self._pipe(reader, remote_writer),
                self._pipe(remote_reader, writer),
                return_exceptions=True,
            )

        except Exception:
            pass
        finally:
            writer.close()

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def run_proxy(
    bind: str = "127.0.0.1",
    port: int = 1080,
    source_address: Optional[str] = None,
):
    proxy = LocalSocks5Proxy(bind, port, source_address)
    await proxy.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await proxy.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VPN-to-SOCKS5 proxy bridge")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1080)
    parser.add_argument("--interface", help="VPN interface name (tun0, wg0, etc)")
    parser.add_argument("--source-ip", help="Source IP to bind outgoing connections")
    parser.add_argument("--list-interfaces", action="store_true")

    args = parser.parse_args()

    if args.list_interfaces:
        ifaces = list_vpn_interfaces()
        if ifaces:
            for iface in ifaces:
                print(f"  {iface['name']}: {iface['ip']}")
        else:
            print("No VPN interfaces found.")
        sys.exit(0)

    source = args.source_ip
    if args.interface and not source:
        ifaces = list_vpn_interfaces()
        for iface in ifaces:
            if iface["name"] == args.interface:
                source = iface["ip"]
                break
        if not source:
            print(f"Interface '{args.interface}' not found or has no IPv4 address")
            sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    asyncio.run(run_proxy(args.bind, args.port, source))
