"""Check that traffic through the Squid proxy goes out via the SSTP VPN.

The script makes the same request twice, directly and through the proxy,
and compares the external IP. Different IPs mean traffic goes through the VPN.
It also opens a WebSocket through the proxy and checks that an echo server
sends the message back.

Usage (the container must be running: docker compose up -d):
    python check_proxy.py
    python check_proxy.py --proxy http://localhost:3128
    python check_proxy.py --ws-url wss://echo.websocket.org
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from urllib.parse import urlparse

IP_INFO_HTTPS = "https://ipinfo.io/json"
IP_INFO_HTTP = "http://ipinfo.io/json"
WS_ECHO_URL = "wss://echo.websocket.org"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455, section 1.3
TIMEOUT = 20


def fetch_ip_info(url, proxy=None):
    """Return (ipinfo data, request time in seconds)."""
    # An empty ProxyHandler disables system proxies for the direct request
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    request = urllib.request.Request(url, headers={"User-Agent": "check_proxy/1.0"})
    started = time.monotonic()
    with opener.open(request, timeout=TIMEOUT) as response:
        data = json.load(response)
    return data, time.monotonic() - started


def describe(data):
    place = ", ".join(filter(None, [data.get("city"), data.get("country")]))
    return f"{data.get('ip')}  ({place or '?'}; {data.get('org', '?')})"


def check(title, url, proxy=None):
    """Make the request and print the result. Return the IP or None."""
    try:
        data, elapsed = fetch_ip_info(url, proxy)
    except Exception as error:  # noqa: BLE001 - any failure reason is useful here
        print(f"  [FAIL] {title:<22} {error}")
        return None
    print(f"  [ OK ] {title:<22} {describe(data)}  [{elapsed:.2f} s]")
    return data.get("ip")


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed by the other side")
        data += chunk
    return data


def recv_http_head(sock):
    """Read an HTTP response head byte by byte, so no WebSocket data is consumed."""
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += recv_exact(sock, 1)
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    parts = status_line.split(" ", 2)
    headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(parts[1]), status_line, headers


def send_text_frame(sock, text):
    """Send a masked text frame (clients must mask, RFC 6455 section 5.3)."""
    payload = text.encode()
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    elif length < 1 << 16:
        header = bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x81, 0x80 | 127]) + length.to_bytes(8, "big")
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


def recv_frame(sock):
    """Return (opcode, payload) of the next frame."""
    first, second = recv_exact(sock, 2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    mask = recv_exact(sock, 4) if second & 0x80 else b""
    payload = recv_exact(sock, length)
    if mask:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return first & 0x0F, payload


def websocket_echo(url, proxy, message):
    """Open a WebSocket through the proxy, send a message and wait for its echo.

    Returns the time in seconds. The proxy only sees an HTTP CONNECT tunnel,
    the WebSocket handshake happens inside it.
    """
    target = urlparse(url)
    secure = target.scheme == "wss"
    port = target.port or (443 if secure else 80)
    proxy_url = urlparse(proxy)
    started = time.monotonic()

    sock = socket.create_connection((proxy_url.hostname, proxy_url.port or 3128), timeout=TIMEOUT)
    try:
        # 1. Ask the proxy for a TCP tunnel to the WebSocket server
        authority = f"{target.hostname}:{port}"
        sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        code, status_line, _ = recv_http_head(sock)
        if code != 200:
            raise ConnectionError(f"proxy refused CONNECT: {status_line}")
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)

        # 2. WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET {target.path or '/'} HTTP/1.1\r\n"
            f"Host: {target.hostname}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: check_proxy/1.0\r\n\r\n"
        ).encode())
        code, status_line, headers = recv_http_head(sock)
        if code != 101:
            # The tunnel is up at this point, so the answer comes from the server itself
            location = f" -> {headers['location']}" if "location" in headers else ""
            raise ConnectionError(f"server rejected the handshake: {status_line}{location}")
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("handshake failed: wrong Sec-WebSocket-Accept")

        # 3. Send the message; the server may send a greeting before the echo
        send_text_frame(sock, message)
        for _ in range(5):
            opcode, payload = recv_frame(sock)
            if opcode == 0x8:
                raise ConnectionError("server closed the WebSocket")
            if opcode == 0x1 and payload.decode(errors="replace") == message:
                return time.monotonic() - started
        raise ConnectionError("echo was not received")
    finally:
        sock.close()


def check_websocket(title, url, proxy):
    """Run the WebSocket echo check and print the result. Return True on success."""
    message = f"check_proxy {os.urandom(4).hex()}"
    try:
        elapsed = websocket_echo(url, proxy, message)
    except Exception as error:  # noqa: BLE001 - any failure reason is useful here
        print(f"  [FAIL] {title:<22} {url}: {error}")
        return False
    print(f"  [ OK ] {title:<22} {url}: sent \"{message}\", got it back  [{elapsed:.2f} s]")
    return True


def proxy_port_open(proxy):
    parsed = urlparse(proxy)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 3128), timeout=5):
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proxy", default="http://localhost:3128",
                        help="proxy address (default: http://localhost:3128)")
    parser.add_argument("--ws-url", default=WS_ECHO_URL,
                        help=f"WebSocket echo server, ws:// or wss:// (default: {WS_ECHO_URL})")
    args = parser.parse_args()

    print(f"Proxy: {args.proxy}\n")

    print("1. Proxy port")
    if not proxy_port_open(args.proxy):
        print("  [FAIL] port is closed: container is not running or VPN is not up yet")
        print("         check: docker compose ps / docker compose logs")
        return 1
    print("  [ OK ] port is open\n")

    print("2. Requests")
    direct_ip = check("direct (HTTPS)", IP_INFO_HTTPS)
    proxy_https_ip = check("via proxy (HTTPS)", IP_INFO_HTTPS, args.proxy)
    proxy_http_ip = check("via proxy (HTTP)", IP_INFO_HTTP, args.proxy)
    print()

    print("3. WebSocket")
    websocket_ok = check_websocket("via proxy (WebSocket)", args.ws_url, args.proxy)
    print()

    print("4. Result")
    if not proxy_https_ip or not proxy_http_ip:
        print("  [FAIL] request through the proxy failed")
        return 1
    if not websocket_ok:
        print("  [FAIL] WebSocket through the proxy failed")
        return 1
    if proxy_https_ip != proxy_http_ip:
        print("  [WARN] HTTP and HTTPS through the proxy came from different IPs")
    if direct_ip is None:
        print(f"  [WARN] direct request failed, nothing to compare; proxy IP: {proxy_https_ip}")
        return 0
    if direct_ip == proxy_https_ip:
        print(f"  [FAIL] same IP ({direct_ip}): traffic bypasses the VPN")
        return 1
    print(f"  [ OK ] traffic goes through the VPN: {direct_ip} -> {proxy_https_ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
