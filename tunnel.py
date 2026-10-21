"""sing-box config for the Steam bypass, sing-box process control and the Steam reachability probe."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import urllib.request
from pathlib import Path

# Vietnamese ISPs block Steam twice: DNS answers are hijacked (even queries to 8.8.8.8) and TLS connections
# are reset by DPI after reading the SNI. Resolving over DoH and splitting the ClientHello into several TLS
# records gets through both, without any tunnel. Game servers are never touched.
STEAM_DOMAINS: tuple[str, ...] = (
    "steampowered.com", "steamcommunity.com", "steamstatic.com", "steamserver.net", "steamcontent.com",
    "steam-chat.com", "steamgames.com", "steamusercontent.com", "valvesoftware.com", "akamaihd.net",
)
STEAM_DOH_SERVER = "1.1.1.1"
STEAM_PROBE_URL = "https://steamcommunity.com/"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def host_has_ipv6() -> bool:
    """True if the OS has a global IPv6 route. UDP connect() only does a route lookup, nothing is sent."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.connect(("2001:4860:4860::8888", 53))
        return True
    except OSError:
        return False


STEAM_TUN_INTERFACE_NAME = "SteamUnblock"
FAKEIP_RANGE = "198.18.0.0/15"


def build_steam_unblock_config(log_path: Path, ipv6: bool) -> dict:
    """
    Steam-only bypass, no VPN. Steam domains get fake IPs, and only that fake range is routed into the TUN,
    so every other packet (games, voice, downloads from other apps) keeps the normal route with zero overhead.
    Inside the TUN, connections to Steam leave directly, resolved over DoH, with the ClientHello split into
    TLS records. All other DNS queries go to the system resolver as before.
    """
    steam = list(STEAM_DOMAINS)
    return {
        "log": {"level": "warn", "timestamp": True, "output": str(log_path)},
        "dns": {
            "servers": [
                {"type": "local", "tag": "dns-local"},
                {"type": "https", "tag": "dns-doh", "server": STEAM_DOH_SERVER},
                {"type": "fakeip", "tag": "dns-fake", "inet4_range": FAKEIP_RANGE},
            ],
            "rules": [
                {"domain_suffix": steam, "query_type": ["A"], "server": "dns-fake"},
                # AAAA would bypass the fake range: answer empty so apps use the IPv4 fake address.
                {"domain_suffix": steam, "action": "predefined", "rcode": "NOERROR"},
            ],
            "final": "dns-local",
            "strategy": "prefer_ipv4" if ipv6 else "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": STEAM_TUN_INTERFACE_NAME,
                "address": ["172.19.1.1/30"],
                "auto_route": True,
                "strict_route": True,
                "route_address": [FAKEIP_RANGE],
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                # Must match by domain: after sniffing, the fake IP destination is rewritten to the domain.
                # TLS only: record fragmentation corrupts plain HTTP.
                {"domain_suffix": steam, "protocol": ["tls"], "outbound": "direct", "tls_record_fragment": True},
            ],
            "final": "direct",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-doh",
        },
    }


class SingBox:
    def __init__(self, core_dir: Path, name: str = "runtime", log_name: str = "sing-box.log"):
        self.exe = core_dir / "sing-box.exe"
        self.config_path = core_dir / f"{name}.json"
        self.log_path = core_dir / log_name
        self._proc: subprocess.Popen | None = None
        # A crash of the previous run can leave its config behind.
        for leftover in (self.config_path, self.config_path.with_suffix(".tmp")):
            leftover.unlink(missing_ok=True)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, config: dict) -> int:
        if self.is_alive():
            return self._proc.pid
        if not self.exe.is_file():
            raise FileNotFoundError(f"{self.exe} not found")

        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.config_path)
        self.log_path.write_text("", encoding="utf-8")

        check = subprocess.run(
            [str(self.exe), "check", "-c", str(self.config_path)],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW,
        )
        if check.returncode != 0:
            raise RuntimeError((check.stderr or check.stdout).strip() or "sing-box check failed")

        self._proc = subprocess.Popen(
            [str(self.exe), "run", "-c", str(self.config_path), "-D", str(self.exe.parent)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        return self._proc.pid

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.config_path.unlink(missing_ok=True)

    def log_tail(self, lines: int = 8) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


_SSL_CTX = ssl.create_default_context()


def probe_steam(timeout: float = 8.0) -> int:
    """HTTP status of a Steam page through the system resolver/route; raises OSError when blocked."""
    req = urllib.request.Request(STEAM_PROBE_URL, headers={"User-Agent": "SteamUnblock"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.status
