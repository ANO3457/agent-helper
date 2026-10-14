"""Browse gateway (docs/decisions/0029, steps 2 and 6): reads web pages in the isolated browser for the service.

Run with `python -m agent_helper.browse_gateway` from the browser image (it needs Playwright). It sits on two
internal networks: the one it shares with the service (`browse-ctl`) and the browser's (`browse-inner`). It
accepts only signed jobs, checks each job again, opens a fresh browser context per job (no cookies, no
storage, no downloads, all traffic through the egress proxy), and returns text, Markdown or a screenshot.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .browse import FORMATS, JOB_PATH, MAX_CHARS, MAX_WAIT_MS, BrowseRefused, check_url
from .config import MIN_ADMIN_SECRET_LENGTH
from .pushcheck import parse_list
from .relay import NONCE, SIGNATURE, SIGNATURE_WINDOW_SECONDS, TIMESTAMP, _SeenSignatures, sign

log = logging.getLogger("agent_helper.browse_gateway")

MAX_REQUEST_BYTES = 8 * 1024
MAX_SUBREQUESTS = 300
MAX_SCREENSHOT_BYTES = 1_500_000
VIEWPORT = {"width": 1280, "height": 800}
READABLE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/json", "application/xml", "text/xml")
# Requests the page makes that are never worth the bandwidth for a reader.
ABORTED_RESOURCE_TYPES = ("media",)
JOB_KEYS = {"job_id", "url", "format", "max_chars", "wait_ms"}
# The driver works to the deadline itself; the gateway cuts it off this much later in any case.
CUTOFF_MARGIN_SECONDS = 5.0

# Visible content of the page as Markdown: headings, paragraphs, links, lists, code, quotes and simple tables.
# Hidden elements, scripts, styles and navigation chrome are skipped.
MARKDOWN_JS = r"""
(maxChars) => {
  const skip = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME", "OBJECT", "EMBED",
                        "HEAD", "BUTTON", "SELECT", "INPUT", "TEXTAREA", "FORM"]);
  const out = [];
  let size = 0;
  const push = (s) => { if (size < maxChars) { out.push(s); size += s.length; } };
  const visible = (el) => {
    const st = window.getComputedStyle(el);
    return st.display !== "none" && st.visibility !== "hidden" && !el.hidden
      && el.getAttribute("aria-hidden") !== "true";
  };
  const inline = (node) => {
    let s = "";
    for (const c of node.childNodes) {
      if (c.nodeType === 3) { s += c.textContent.replace(/\s+/g, " "); continue; }
      if (c.nodeType !== 1 || skip.has(c.tagName) || !visible(c)) continue;
      const t = c.tagName;
      if (t === "A") {
        const text = inline(c).trim();
        const href = c.href || "";
        s += href.startsWith("http") && text ? `[${text}](${href})` : text;
      } else if (t === "STRONG" || t === "B") { const x = inline(c).trim(); s += x ? `**${x}**` : ""; }
      else if (t === "EM" || t === "I") { const x = inline(c).trim(); s += x ? `*${x}*` : ""; }
      else if (t === "CODE") { s += "`" + c.textContent + "`"; }
      else if (t === "BR") { s += "\n"; }
      else if (t === "IMG") { const alt = (c.getAttribute("alt") || "").trim(); s += alt ? `[image: ${alt}]` : ""; }
      else { s += inline(c); }
    }
    return s;
  };
  const block = (node, depth) => {
    for (const c of node.childNodes) {
      if (size >= maxChars) return;
      if (c.nodeType === 3) { const t = c.textContent.replace(/\s+/g, " ").trim(); if (t) push(t + "\n\n"); continue; }
      if (c.nodeType !== 1 || skip.has(c.tagName) || !visible(c)) continue;
      const t = c.tagName;
      if (/^H[1-6]$/.test(t)) { const x = inline(c).trim(); if (x) push("#".repeat(+t[1]) + " " + x + "\n\n"); }
      else if (t === "P") { const x = inline(c).trim(); if (x) push(x + "\n\n"); }
      else if (t === "PRE") { push("```\n" + c.innerText.replace(/\n+$/, "") + "\n```\n\n"); }
      else if (t === "BLOCKQUOTE") {
        const x = c.innerText.trim();
        if (x) push(x.split("\n").map(l => "> " + l).join("\n") + "\n\n");
      }
      else if (t === "UL" || t === "OL") {
        let i = 1;
        for (const li of c.children) {
          if (li.tagName !== "LI" || !visible(li)) continue;
          const x = inline(li).trim();
          if (x) push("  ".repeat(depth) + (t === "OL" ? `${i++}. ` : "- ") + x.replace(/\n+/g, " ") + "\n");
        }
        push("\n");
      } else if (t === "TABLE") {
        const rows = [...c.querySelectorAll("tr")].slice(0, 200).map(r =>
          [...r.children].map(cell => inline(cell).trim().replace(/\|/g, "\\|").replace(/\n+/g, " ")));
        if (rows.length) {
          const width = Math.max(...rows.map(r => r.length));
          const line = (r) => "| " + [...r, ...Array(width - r.length).fill("")].join(" | ") + " |\n";
          push(line(rows[0]) + "|" + " --- |".repeat(width) + "\n" + rows.slice(1).map(line).join("") + "\n");
        }
      } else if (t === "HR") { push("---\n\n"); }
      else { block(c, t === "LI" ? depth + 1 : depth); }
    }
  };
  if (document.body) block(document.body, 0);
  return out.join("").replace(/\n{3,}/g, "\n\n").trim();
}
"""
TEXT_JS = "() => (document.body ? document.body.innerText : (document.documentElement.textContent || '')).trim()"


class Driver(Protocol):
    async def read(self, job: dict[str, Any], deadline: float) -> dict[str, Any]: ...


def valid_job(job: Any) -> dict[str, Any]:
    """The job as the gateway will run it; the same rules as in the service, checked again. Raises ValueError."""
    if not isinstance(job, dict) or set(job) != JOB_KEYS:
        raise ValueError("unexpected job fields")
    if not isinstance(job["job_id"], str) or not 1 <= len(job["job_id"]) <= 64:
        raise ValueError("bad job id")
    if job["format"] not in FORMATS:
        raise ValueError("bad format")
    for name, low, high in (("max_chars", 100, MAX_CHARS), ("wait_ms", 0, MAX_WAIT_MS)):
        value = job[name]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f"bad {name}")
    return job


def _readable(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() in READABLE_TYPES or content_type.lower().startswith("text/")


class PlaywrightDriver:
    """Drives the Camoufox Playwright server over its WebSocket endpoint. One connection, reopened when lost;
    one fresh context per job."""

    def __init__(self, ws_endpoint: str, proxy_server: str) -> None:
        self.ws_endpoint = ws_endpoint
        self.proxy_server = proxy_server
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> Any:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright  # only in the browser image

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.firefox.connect(self.ws_endpoint, timeout=15_000)
            return self._browser

    async def read(self, job: dict[str, Any], deadline: float) -> dict[str, Any]:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        def left_ms() -> float:
            return max(1.0, (deadline - time.monotonic()) * 1000)

        browser = await self._connect()
        context = await browser.new_context(
            accept_downloads=False,
            proxy={"server": self.proxy_server},
            viewport=VIEWPORT,
            service_workers="block",
            locale="en-US",
            timezone_id="UTC",
            ignore_https_errors=False,
        )
        try:
            seen = 0

            async def route(handled: Any) -> None:
                nonlocal seen
                seen += 1
                request = handled.request
                scheme = urlsplit(request.url).scheme.lower()
                if (
                    seen > MAX_SUBREQUESTS
                    or request.resource_type in ABORTED_RESOURCE_TYPES
                    or scheme
                    not in (
                        "http",
                        "https",
                    )
                ):
                    await handled.abort()
                else:
                    await handled.continue_()

            await context.route("**/*", route)
            page = await context.new_page()
            try:
                response = await page.goto(job["url"], wait_until="domcontentloaded", timeout=left_ms())
            except PlaywrightTimeout:
                return {"outcome": "timeout"}
            except PlaywrightError:
                # Refused by the proxy, unreachable, TLS failure or a download: one answer for all.
                return {"outcome": "failed"}
            if response is None or not _readable(await response.header_value("content-type") or ""):
                return {"outcome": "failed"}
            try:
                await page.wait_for_load_state("networkidle", timeout=min(3000.0, left_ms()))
            except PlaywrightTimeout:
                pass  # many pages never go idle; read what is there
            if job["wait_ms"]:
                await page.wait_for_timeout(min(job["wait_ms"], left_ms()))
            final_url = page.url if urlsplit(page.url).scheme in ("http", "https") else job["url"]
            result: dict[str, Any] = {
                "outcome": "ok",
                "status": response.status,
                "final_url": final_url,
                "title": (await page.title())[:500],
            }
            if job["format"] == "screenshot":
                image = await page.screenshot(type="jpeg", quality=60, full_page=False, timeout=left_ms())
                if len(image) > MAX_SCREENSHOT_BYTES:
                    image = await page.screenshot(type="jpeg", quality=30, full_page=False, timeout=left_ms())
                if len(image) > MAX_SCREENSHOT_BYTES:
                    return {"outcome": "failed"}
                result["image_base64"] = base64.b64encode(image).decode("ascii")
            else:
                script = MARKDOWN_JS if job["format"] == "markdown" else TEXT_JS
                arg = job["max_chars"] + 1
                content = (
                    await page.evaluate(script, arg) if job["format"] == "markdown" else await page.evaluate(script)
                )
                content = content if isinstance(content, str) else ""
                result["truncated"] = len(content) > job["max_chars"]
                result["content"] = content[: job["max_chars"]]
            return result
        except PlaywrightTimeout:
            return {"outcome": "timeout"}
        except PlaywrightError:
            return {"outcome": "failed"}
        finally:
            try:
                await asyncio.wait_for(context.close(), 5)
            except Exception:  # noqa: BLE001 (a context that will not close is dropped with the connection)
                log.warning("browser context did not close; reconnecting")
                self._browser = None


def create_gateway_app(
    secret: str,
    driver: Driver,
    *,
    concurrency: int = 2,
    timeout: float = 30.0,
    deny_domains: tuple[str, ...] = (),
    own_hosts: tuple[str, ...] = (),
) -> FastAPI:
    if len(secret) < MIN_ADMIN_SECRET_LENGTH:
        raise SystemExit(f"BROWSE_SECRET must be at least {MIN_ADMIN_SECRET_LENGTH} characters")
    seen = _SeenSignatures()
    slots = asyncio.Semaphore(concurrency)
    app = FastAPI(
        title="agent-helper browse gateway", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )

    async def authenticate(request: Request) -> bytes:
        timestamp = request.headers.get("x-relay-timestamp", "")
        given = request.headers.get("x-relay-signature", "")
        nonce = request.headers.get("x-relay-nonce", "")
        if not NONCE.fullmatch(nonce) or not TIMESTAMP.fullmatch(timestamp) or not SIGNATURE.fullmatch(given):
            raise HTTPException(401, "unauthorized")
        if abs(time.time() - int(timestamp)) > SIGNATURE_WINDOW_SECONDS:
            raise HTTPException(401, "unauthorized")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "too large")
        expected = sign(secret, timestamp, nonce, request.method, request.url.path, body)
        if request.url.query or not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "unauthorized")
        if not seen.first_time(given, time.time()):
            raise HTTPException(401, "unauthorized")
        return body

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(JOB_PATH)
    async def run(request: Request) -> JSONResponse:
        body = await authenticate(request)
        try:
            job = valid_job(json.loads(body))
            target = check_url(job["url"], deny_domains, own_hosts)
        except (ValueError, BrowseRefused):
            return JSONResponse({"outcome": "refused"}, status_code=400)
        if slots.locked():
            return JSONResponse({"outcome": "busy"}, status_code=503)
        async with slots:
            deadline = time.monotonic() + timeout
            try:
                result = await asyncio.wait_for(
                    driver.read({**job, "url": target.url}, deadline), timeout + CUTOFF_MARGIN_SECONDS
                )
            except TimeoutError:
                result = {"outcome": "timeout"}
            except Exception:  # noqa: BLE001 (a browser failure is an answer, not a crash)
                log.exception("browse job failed unexpectedly")
                result = {"outcome": "failed"}
        log.info("job %s %s %s", target.domain, job["format"], result.get("outcome"))
        return JSONResponse(result)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ws_path = os.environ.get("BROWSER_WS_PATH", "")
    if len(ws_path) < 16:
        raise SystemExit("BROWSER_WS_PATH must be a random path of at least 16 characters")
    host = os.environ.get("BROWSER_HOST", "browser")
    port = int(os.environ.get("BROWSER_PORT", "9222"))
    driver = PlaywrightDriver(
        f"ws://{host}:{port}/{ws_path.strip('/')}", os.environ.get("EGRESS_PROXY", "http://egress:3128")
    )
    own = urlsplit(os.environ.get("PUBLIC_BASE_URL", "")).hostname
    app = create_gateway_app(
        os.environ.get("BROWSE_SECRET", ""),
        driver,
        concurrency=int(os.environ.get("BROWSE_CONCURRENCY", "2")),
        timeout=float(os.environ.get("BROWSE_TIMEOUT_SECONDS", "30")),
        deny_domains=parse_list(os.environ.get("BROWSE_DENY_DOMAINS", "")),
        own_hosts=(own.lower(),) if own else (),
    )
    uvicorn.run(
        app,
        host=os.environ.get("GATEWAY_LISTEN_HOST", "0.0.0.0"),  # noqa: S104 (internal networks only)
        port=int(os.environ.get("GATEWAY_LISTEN_PORT", "8095")),
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
