"""Wi-Fi AP + captive-portal fallback for the focus-assist page.

Triggered by cam-boot.service (see camrig.boot) when there's no internet after
boot: a fresh deployment, wrong Wi-Fi credentials, or a site out of Tailscale's
reach. In that state the only recourse would otherwise be pulling the SD card
or plugging in a monitor. Instead this stands wlan0 up as its own open AP
("camrig-setup" by default) for a bounded, idle-timed-out window, with DNS
wildcarded to the Pi so a phone that joins gets dropped straight onto the
existing camrig.focus page -- real captive-portal behaviour, since most
phones/laptops auto-open a sign-in browser once their connectivity probe gets
an unexpected response.

Built on nmcli (this fleet runs NetworkManager -- see docs/basler-gige.md)
with ipv4.method=manual, deliberately not NM's "shared" method: shared spawns
NM's own internal dnsmasq for DHCP+DNS forwarding, which would then have to be
fought to get the wildcard-redirect a captive portal needs. Manual only sets
the interface IP; DHCP + wildcard DNS is our own dnsmasq instance, scoped to
wlan0 only (the same approach balena's wifi-connect and comitup use).
"""

from __future__ import annotations

import logging
import shlex
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

from .config import BaslerConfig, CaptiveConfig, CaptureConfig
from .focus import FocusConfig, _Handler, build_focus_commands, start_stream, stop_stream

log = logging.getLogger("camrig.captive")

_AP_IFACE = "wlan0"
_AP_CON = "camrig-ap"
_POLL_SECONDS = 5


def internet_reachable(timeout: float = 3.0) -> bool:
    """TCP-connect probe to well-known IPs (no DNS dependency)."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _subnet_ip(ap_ip: str, last_octet: str) -> str:
    return ".".join(ap_ip.split(".")[:3] + [last_octet])


def start_ap(cfg: CaptiveConfig) -> None:
    subprocess.run(["nmcli", "connection", "delete", _AP_CON], capture_output=True)
    add_cmd = [
        "nmcli", "connection", "add", "type", "wifi", "ifname", _AP_IFACE,
        "con-name", _AP_CON, "autoconnect", "no", "ssid", cfg.ssid,
        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
        "ipv4.method", "manual", "ipv4.addresses", f"{cfg.ap_ip}/24",
        "ipv4.gateway", cfg.ap_ip, "ipv4.ignore-auto-dns", "yes",
        "ipv6.method", "disabled",
    ]
    if cfg.psk:
        add_cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", cfg.psk]
    else:
        add_cmd += ["wifi-sec.key-mgmt", "none"]
    subprocess.run(add_cmd, check=True)
    subprocess.run(["nmcli", "connection", "up", _AP_CON], check=True)
    log.info("AP up: ssid=%s ip=%s", cfg.ssid, cfg.ap_ip)


def stop_ap() -> None:
    subprocess.run(["nmcli", "connection", "down", _AP_CON], capture_output=True)
    subprocess.run(["nmcli", "connection", "delete", _AP_CON], capture_output=True)
    log.info("AP down")


def start_dnsmasq(cfg: CaptiveConfig) -> subprocess.Popen:
    """DHCP + wildcard-DNS server bound only to the AP interface.

    ``--address=/#/<ap_ip>`` resolves every hostname to the Pi -- the actual
    captive-portal-redirect mechanism.
    """
    args = [
        "dnsmasq", "--keep-in-foreground",
        f"--interface={_AP_IFACE}", "--bind-interfaces", "--except-interface=lo",
        "--no-resolv", "--no-hosts",
        f"--dhcp-range={_subnet_ip(cfg.ap_ip, '10')},{_subnet_ip(cfg.ap_ip, '50')},12h",
        f"--dhcp-option=option:router,{cfg.ap_ip}",
        f"--dhcp-option=option:dns-server,{cfg.ap_ip}",
        f"--address=/#/{cfg.ap_ip}",
    ]
    log.info("dnsmasq: %s", shlex.join(args))
    return subprocess.Popen(args)


def stop_dnsmasq(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run(
    cfg: CaptiveConfig,
    *,
    capture: CaptureConfig,
    basler: BaslerConfig | None = None,
    dry_run: bool = False,
) -> int:
    focus_cfg = FocusConfig.from_capture(capture, port=cfg.port)

    if dry_run:
        rendered = " | ".join(shlex.join(cmd) for cmd in build_focus_commands(focus_cfg, basler))
        print(f"nmcli connection up {_AP_CON}  # ssid={cfg.ssid} ip={cfg.ap_ip}")
        print(f"dnsmasq --interface={_AP_IFACE} --address=/#/{cfg.ap_ip} ...")
        print(rendered)
        return 0

    if internet_reachable():
        log.info("Internet reachable; captive portal not needed")
        return 0

    start_ap(cfg)
    dnsmasq_proc = start_dnsmasq(cfg)
    procs, buffer, _reader = start_stream(focus_cfg, basler)

    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), _Handler)
    server.buffer = buffer  # type: ignore[attr-defined]
    server.catch_all = True  # type: ignore[attr-defined]
    server.last_request = time.monotonic()  # type: ignore[attr-defined]
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"\ncamrig captive-portal — join Wi-Fi \"{cfg.ssid}\", any page redirects to the focus view.")
    print(f"Direct URL: http://{cfg.ap_ip}:{cfg.port}/")
    print(f"Idle timeout: {cfg.timeout_minutes} min. Ctrl-C to stop now.\n")

    timeout_s = cfg.timeout_minutes * 60
    try:
        while time.monotonic() - server.last_request < timeout_s:  # type: ignore[attr-defined]
            time.sleep(_POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    else:
        log.info("Idle timeout (%d min); tearing down captive portal", cfg.timeout_minutes)
    finally:
        server.shutdown()
        stop_stream(procs, buffer)
        stop_dnsmasq(dnsmasq_proc)
        stop_ap()
    return 0
