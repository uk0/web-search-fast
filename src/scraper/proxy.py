"""Thread-safe round-robin proxy rotator with local bridge for SOCKS5 auth.

Firefox/Playwright doesn't support SOCKS5 with authentication directly.
This module provides a local HTTP CONNECT proxy bridge that authenticates
to upstream SOCKS5(H) proxies, making them usable with Playwright.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import socket
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Port range for local proxy bridges
_LOCAL_PORT_START = 19100


def _parse_socks_url(url: str) -> dict:
    """Parse a socks5(h)://user:pass@host:port URL."""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 1080,
        "username": parsed.username or "",
        "password": parsed.password or "",
        "remote_dns": url.startswith("socks5h://"),
    }


class _Socks5Bridge:
    """Async local HTTP CONNECT proxy that forwards via SOCKS5 with auth."""

    def __init__(self, upstream: str, local_port: int) -> None:
        self._upstream = _parse_socks_url(upstream)
        self._local_port = local_port
        self._server: asyncio.AbstractServer | None = None

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self._local_port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self._local_port,
        )
        logger.debug("[bridge] listening on 127.0.0.1:%d → %s:%d",
                      self._local_port, self._upstream["host"], self._upstream["port"])

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle an incoming HTTP CONNECT request."""
        try:
            # Read the HTTP CONNECT request
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                writer.close()
                return

            line = request_line.decode("utf-8", errors="replace").strip()
            # Read remaining headers
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=5)
                if header in (b"\r\n", b"\n", b""):
                    break

            if not line.startswith("CONNECT "):
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            # Parse CONNECT host:port
            parts = line.split()
            if len(parts) < 2:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            target = parts[1]
            if ":" in target:
                target_host, target_port_str = target.rsplit(":", 1)
                target_port = int(target_port_str)
            else:
                target_host = target
                target_port = 443

            # Connect to upstream SOCKS5 proxy
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self._socks5_connect(target_host, target_port),
                    timeout=15,
                )
            except Exception as exc:
                logger.warning("[bridge] SOCKS5 connect failed for %s:%d: %s", target_host, target_port, exc)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            # Send 200 Connection Established
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            # Bidirectional relay
            await self._relay(reader, writer, upstream_reader, upstream_writer)

        except Exception as exc:
            logger.debug("[bridge] client handler error: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _socks5_connect(self, target_host: str, target_port: int) -> tuple:
        """Establish a SOCKS5 connection with auth to the upstream proxy."""
        up = self._upstream
        reader, writer = await asyncio.open_connection(up["host"], up["port"])

        try:
            # SOCKS5 greeting: version=5, 1 auth method (username/password=0x02)
            if up["username"]:
                writer.write(b"\x05\x01\x02")
            else:
                writer.write(b"\x05\x01\x00")
            await writer.drain()

            resp = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            if resp[0] != 0x05:
                raise RuntimeError(f"SOCKS5 version mismatch: {resp[0]}")

            # Auth if needed
            if resp[1] == 0x02 and up["username"]:
                uname = up["username"].encode()
                passwd = up["password"].encode()
                writer.write(b"\x01" + bytes([len(uname)]) + uname + bytes([len(passwd)]) + passwd)
                await writer.drain()
                auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=10)
                if auth_resp[1] != 0x00:
                    raise RuntimeError("SOCKS5 auth failed")
            elif resp[1] == 0xFF:
                raise RuntimeError("SOCKS5 no acceptable auth method")

            # CONNECT request
            if up["remote_dns"]:
                # Domain name (ATYP=0x03)
                host_bytes = target_host.encode()
                writer.write(
                    b"\x05\x01\x00\x03"
                    + bytes([len(host_bytes)]) + host_bytes
                    + struct.pack("!H", target_port)
                )
            else:
                # Try IP address first
                import socket
                try:
                    addr = socket.inet_aton(target_host)
                    writer.write(b"\x05\x01\x00\x01" + addr + struct.pack("!H", target_port))
                except OSError:
                    # Fall back to domain
                    host_bytes = target_host.encode()
                    writer.write(
                        b"\x05\x01\x00\x03"
                        + bytes([len(host_bytes)]) + host_bytes
                        + struct.pack("!H", target_port)
                    )
            await writer.drain()

            # Read CONNECT response (at least 4 bytes header)
            resp = await asyncio.wait_for(reader.readexactly(4), timeout=10)
            if resp[1] != 0x00:
                raise RuntimeError(f"SOCKS5 connect failed: status={resp[1]}")

            # Read remaining address bytes based on ATYP
            atyp = resp[3]
            if atyp == 0x01:  # IPv4
                await reader.readexactly(4 + 2)
            elif atyp == 0x03:  # Domain
                domain_len = (await reader.readexactly(1))[0]
                await reader.readexactly(domain_len + 2)
            elif atyp == 0x04:  # IPv6
                await reader.readexactly(16 + 2)

            return reader, writer

        except Exception:
            writer.close()
            raise

    @staticmethod
    async def _relay(
        c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional data relay between client and upstream."""
        async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        t1 = asyncio.create_task(_pipe(c_reader, u_writer))
        t2 = asyncio.create_task(_pipe(u_reader, c_writer))
        await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        t1.cancel()
        t2.cancel()


class ProxyRotator:
    """Round-robin proxy rotator with local bridge support for SOCKS5 auth.

    For SOCKS5(H) proxies with authentication, starts local HTTP CONNECT
    bridges since Firefox/Playwright doesn't support SOCKS5 auth natively.
    """

    def __init__(self, proxies: list[str]) -> None:
        if not proxies:
            raise ValueError("Proxy list must not be empty")
        self._proxies = list(proxies)
        self._index = 0
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._bridges: list[_Socks5Bridge] = []
        self._bridge_urls: list[str] = []
        self._bridges_started = False
        logger.info("[proxy] initialized with %d proxies", len(self._proxies))

    async def start_bridges(self) -> None:
        """Start local HTTP bridges for SOCKS5 proxies that need auth."""
        if self._bridges_started:
            return
        for i, proxy in enumerate(self._proxies):
            parsed = urlparse(proxy)
            needs_bridge = (
                parsed.scheme in ("socks5", "socks5h")
                and parsed.username
            )
            if needs_bridge:
                port = _LOCAL_PORT_START + i
                bridge = _Socks5Bridge(proxy, port)
                await bridge.start()
                self._bridges.append(bridge)
                self._bridge_urls.append(bridge.local_url)
            else:
                self._bridges.append(None)  # type: ignore[arg-type]
                # For non-auth proxies, convert socks5h→socks5 for Playwright
                url = proxy
                if url.startswith("socks5h://"):
                    url = "socks5://" + url[len("socks5h://"):]
                self._bridge_urls.append(url)
        self._bridges_started = True
        active = sum(1 for b in self._bridges if b is not None)
        logger.info("[proxy] started %d local bridges for %d proxies", active, len(self._proxies))

    async def stop_bridges(self) -> None:
        """Stop all local bridges."""
        for bridge in self._bridges:
            if bridge is not None:
                await bridge.stop()
        self._bridges.clear()
        self._bridge_urls.clear()
        self._bridges_started = False

    def next(self) -> dict:
        """Return the next proxy as a Playwright proxy config dict.

        Returns {"server": url, "_original_url": ...} for simple proxies, or
        {"server": url, "username": ..., "password": ..., "_original_url": ...} for auth proxies.
        SOCKS5 auth proxies return the local bridge URL (no credentials needed).
        The "_original_url" key holds the raw proxy URL for DB tracking.
        """
        with self._lock:
            idx = self._index % len(self._proxies)
            self._index += 1
            original_url = self._proxies[idx]

            if self._bridges_started and self._bridge_urls:
                bridge_url = self._bridge_urls[idx]
                parsed = urlparse(original_url)
                # SOCKS5 bridges strip auth — just return server URL
                # HTTP(S) proxies with auth — extract credentials for Playwright
                if parsed.scheme in ("http", "https") and parsed.username:
                    # Strip credentials from URL for the server field
                    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                    return {
                        "server": server,
                        "username": parsed.username,
                        "password": parsed.password or "",
                        "_original_url": original_url,
                    }
                return {"server": bridge_url, "_original_url": original_url}

            # Fallback: direct URL with socks5h→socks5 conversion
            proxy = original_url
            parsed = urlparse(proxy)
            if proxy.startswith("socks5h://"):
                proxy = "socks5://" + proxy[len("socks5h://"):]
                parsed = urlparse(proxy)

            # Extract auth for HTTP(S) proxies
            if parsed.scheme in ("http", "https") and parsed.username:
                server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                return {
                    "server": server,
                    "username": parsed.username,
                    "password": parsed.password or "",
                    "_original_url": original_url,
                }
            return {"server": proxy, "_original_url": original_url}

    def mark_failed(self, proxy: str) -> None:
        """Record a failure for a proxy."""
        with self._lock:
            self._failures[proxy] = self._failures.get(proxy, 0) + 1
            logger.warning(
                "[proxy] marked failed: %s...%s (total=%d)",
                proxy[:30], proxy[-10:], self._failures[proxy],
            )

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def failure_stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._failures)

    @classmethod
    def from_file(cls, path: str) -> ProxyRotator:
        """Load proxies from a text file (one per line, skip blanks/comments)."""
        lines = Path(path).read_text().splitlines()
        proxies = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        if not proxies:
            raise ValueError(f"No proxies found in {path}")
        logger.info("[proxy] loaded %d proxies from %s", len(proxies), path)
        return cls(proxies)


# ---------------------------------------------------------------------------
# Proxy reachability tester
# ---------------------------------------------------------------------------


async def _http_connect_test(
    proxy_host: str, proxy_port: int,
    proxy_user: str | None, proxy_pass: str | None,
    target_host: str, target_port: int, timeout: float,
) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout=timeout,
    )
    try:
        auth_header = b""
        if proxy_user:
            creds = base64.b64encode(
                f"{proxy_user}:{proxy_pass or ''}".encode()
            ).decode()
            auth_header = f"Proxy-Authorization: Basic {creds}\r\n".encode()
        req = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
        ).encode() + auth_header + b"\r\n"
        writer.write(req)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        status_line = line.decode("ascii", errors="replace").strip()
        if " 200 " not in status_line and not status_line.endswith(" 200"):
            raise RuntimeError(f"HTTP CONNECT failed: {status_line or 'empty response'}")
        while True:
            header = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if header in (b"\r\n", b"\n", b""):
                break
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _socks5_test(
    proxy_host: str, proxy_port: int,
    proxy_user: str | None, proxy_pass: str | None,
    target_host: str, target_port: int, timeout: float, remote_dns: bool,
) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout=timeout,
    )
    try:
        if proxy_user:
            writer.write(b"\x05\x01\x02")
        else:
            writer.write(b"\x05\x01\x00")
        await writer.drain()

        resp = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if resp[0] != 0x05:
            raise RuntimeError(f"SOCKS5 version mismatch: {resp[0]}")

        if resp[1] == 0x02 and proxy_user:
            uname = proxy_user.encode()
            passwd = (proxy_pass or "").encode()
            writer.write(
                b"\x01" + bytes([len(uname)]) + uname
                + bytes([len(passwd)]) + passwd
            )
            await writer.drain()
            auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if auth_resp[1] != 0x00:
                raise RuntimeError("SOCKS5 auth failed")
        elif resp[1] == 0xFF:
            raise RuntimeError("SOCKS5 no acceptable auth method")

        if remote_dns:
            host_bytes = target_host.encode()
            writer.write(
                b"\x05\x01\x00\x03"
                + bytes([len(host_bytes)]) + host_bytes
                + struct.pack("!H", target_port)
            )
        else:
            try:
                addr = socket.inet_aton(target_host)
                writer.write(b"\x05\x01\x00\x01" + addr + struct.pack("!H", target_port))
            except OSError:
                host_bytes = target_host.encode()
                writer.write(
                    b"\x05\x01\x00\x03"
                    + bytes([len(host_bytes)]) + host_bytes
                    + struct.pack("!H", target_port)
                )
        await writer.drain()

        resp = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if resp[1] != 0x00:
            raise RuntimeError(f"SOCKS5 connect failed: status={resp[1]}")

        atyp = resp[3]
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout=timeout)
        elif atyp == 0x03:
            domain_len = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
            await asyncio.wait_for(reader.readexactly(domain_len + 2), timeout=timeout)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout=timeout)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def test_proxy(
    url: str,
    target_host: str = "www.google.com",
    target_port: int = 443,
    timeout: float = 8.0,
) -> dict:
    """Probe a proxy by establishing a tunneled TCP connection to target.

    Returns: {"ok": bool, "latency_ms": int, "error": str | None}
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    proxy_host = parsed.hostname or ""
    if not proxy_host:
        return {"ok": False, "latency_ms": 0, "error": "missing proxy host"}
    proxy_port = parsed.port or (1080 if scheme.startswith("socks") else 8080)

    start = time.monotonic()
    try:
        if scheme in ("http", "https"):
            await _http_connect_test(
                proxy_host, proxy_port, parsed.username, parsed.password,
                target_host, target_port, timeout,
            )
        elif scheme in ("socks5", "socks5h"):
            await _socks5_test(
                proxy_host, proxy_port, parsed.username, parsed.password,
                target_host, target_port, timeout, remote_dns=(scheme == "socks5h"),
            )
        else:
            return {"ok": False, "latency_ms": 0, "error": f"unsupported scheme: {scheme or '?'}"}
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"ok": True, "latency_ms": latency_ms, "error": None}
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(exc) or exc.__class__.__name__,
        }
