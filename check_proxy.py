"""Check that traffic through the Squid proxy goes out via the SSTP VPN.

The script makes the same request twice, directly and through the proxy,
and compares the external IP. Different IPs mean traffic goes through the VPN.

Usage (the container must be running: docker compose up -d):
    python check_proxy.py
    python check_proxy.py --proxy http://localhost:3128
"""

import argparse
import json
import socket
import sys
import time
import urllib.request
from urllib.parse import urlparse

IP_INFO_HTTPS = "https://ipinfo.io/json"
IP_INFO_HTTP = "http://ipinfo.io/json"
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

    print("3. Result")
    if not proxy_https_ip or not proxy_http_ip:
        print("  [FAIL] request through the proxy failed")
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
