"""The isolated browser for the web page reader (docs/decisions/0029, step 2).

Run with `python -m agent_helper.browser_server` in the browser image. It starts Camoufox as a Playwright
server on a secret WebSocket path. Its container sits only on an internal network with the egress proxy and
the gateway; the preferences below are the first layer, the network layout the second.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Firefox preferences: every connection through the proxy (localhost included), nothing that opens a
# connection or a channel the proxy cannot see, nothing that keeps state or opens files.
FIREFOX_PREFS: dict[str, object] = {
    "network.proxy.allow_hijacking_localhost": True,
    "network.proxy.no_proxies_on": "",
    "network.proxy.failover_direct": False,
    "network.proxy.socks_remote_dns": True,
    "network.trr.mode": 5,  # no DNS over HTTPS
    "network.dns.disablePrefetch": True,
    "network.dns.disablePrefetchFromHTTPS": True,
    "network.prefetch-next": False,
    "network.predictor.enabled": False,
    "network.http.speculative-parallel-limit": 0,
    "media.peerconnection.enabled": False,  # no WebRTC
    "media.navigator.enabled": False,
    "dom.serviceWorkers.enabled": False,
    "dom.webnotifications.enabled": False,
    "dom.push.enabled": False,
    "geo.enabled": False,
    "pdfjs.disabled": True,  # PDFs are downloads, and downloads are refused
    "browser.download.open_pdf_attachments_inline": False,
    "network.protocol-handler.external-default": False,
    "network.protocol-handler.warn-external-default": True,
    "browser.cache.disk.enable": False,
    "browser.sessionstore.resume_from_crash": False,
    "signon.rememberSignons": False,
    "extensions.update.enabled": False,
    "app.update.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.enabled": False,
    "browser.safebrowsing.malware.enabled": False,  # would call out to update lists
    "browser.safebrowsing.phishing.enabled": False,
    "captivedetect.canonicalURL": "",
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
}


def verify_build(expected_sha256: str, install_dir: Path | None = None) -> None:
    """Fail the image build unless the fetched browser is exactly the pinned one. `camoufox fetch` checks the
    download against the digest GitHub reports; this checks that digest against the one recorded in
    Dockerfile.browser, so a changed release asset cannot slip into a rebuild."""
    if install_dir is None:
        from camoufox.pkgman import INSTALL_DIR  # only in the browser image

        install_dir = Path(INSTALL_DIR)
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise SystemExit("the expected browser digest must be 64 hex digits")
    found = []
    for version_file in sorted(install_dir.rglob("version.json")):
        try:
            found.append(str(json.loads(version_file.read_text("utf-8")).get("sha256") or "").lower())
        except (OSError, ValueError):
            continue
    if expected not in found:
        raise SystemExit(f"the fetched browser build does not match the pinned digest (found: {found or 'none'})")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-build":
        verify_build(sys.argv[2])
        print("browser build matches the pinned digest")
        return
    from camoufox.server import launch_server  # only in the browser image

    ws_path = os.environ.get("BROWSER_WS_PATH", "")
    if len(ws_path) < 16:
        raise SystemExit("BROWSER_WS_PATH must be a random path of at least 16 characters")
    launch_server(
        headless=True,
        port=int(os.environ.get("BROWSER_PORT", "9222")),
        ws_path=ws_path.strip("/"),
        # Every page goes through the egress proxy; the gateway sets the same proxy on each context.
        proxy={"server": os.environ.get("EGRESS_PROXY", "http://egress:3128")},
        geoip=False,  # a lookup of the exit address would be one more outbound call
        locale="en-US",
        block_webrtc=True,
        firefox_user_prefs=FIREFOX_PREFS,
        i_know_what_im_doing=True,
    )


if __name__ == "__main__":
    main()
