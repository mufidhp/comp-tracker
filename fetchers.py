"""
fetchers.py — the fetch layer.

Responsibilities:
  * Fetch each source with a realistic browser identity + retries/backoff.
  * Fall back to headless Chromium (Playwright) when plain HTTP is blocked or
    the page is JavaScript-rendered.
  * Cache-bust Telegram URLs.
  * Return an honest health record per source: ok / blocked / failed / empty
    (the 'stale' verdict is added later once we know the newest item date).

Nothing here parses competition data — that's parsers.py.
"""
from __future__ import annotations

import time
import datetime as dt
from urllib.parse import urlparse, urlencode

import requests

UTC = dt.timezone.utc


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat()


def _headers(cfg: dict, referer: str | None = None) -> dict:
    fc = cfg.get("fetch", {})
    h = {
        "User-Agent": fc.get("user_agent", "Mozilla/5.0"),
        "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": fc.get("accept_language", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        h["Referer"] = referer
    return h


def _cache_bust(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode({'_cb': int(time.time())})}"


_BLOCK_MARKERS = ("access denied", "permission to access", "attention required",
                  "cf-error", "just a moment", "captcha", "forbidden",
                  "errors.edgesuite.net", "request blocked")


def _looks_blocked(content: str) -> bool:
    """Detect a challenge / access-denied stub masquerading as a 200."""
    if not content:
        return True
    low = content[:2000].lower()
    if any(m in low for m in _BLOCK_MARKERS):
        return True
    # a page with essentially no markup is almost always a block/soft-404
    if len(content.strip()) < 600 and "<a" not in low:
        return True
    return False


def _health(source: dict, status: str, error: str | None = None) -> dict:
    return {
        "source": source.get("name", source.get("venue", "?")),
        "venue": source.get("venue", ""),
        "method": source.get("method", ""),
        "reliability": source.get("reliability", ""),
        "status": status,               # ok | blocked | failed | empty | stale | disabled
        "fetched_at": now_iso(),
        "newest_item_date": None,       # filled by scanner after parsing
        "url": source.get("url", ""),
        "error": error,
    }


# ------------------------------------------------------------------ HTTP
def _requests_get(url: str, cfg: dict, referer: str | None = None) -> requests.Response:
    fc = cfg.get("fetch", {})
    timeout = fc.get("timeout_sec", 25)
    retries = int(fc.get("retries", 3))
    backoff = float(fc.get("backoff_base_sec", 2))
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_headers(cfg, referer), timeout=timeout)
            # Retry only on transient throttling; return everything else to caller.
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                time.sleep(backoff * (2 ** attempt))
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(backoff * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise requests.RequestException("unknown fetch failure")


# ------------------------------------------------------------------ Playwright
_PW_UNAVAILABLE = None  # cache the import failure reason


def _playwright_get(url: str, cfg: dict, wait_selector: str | None = None) -> str:
    """Render a page (or JSON endpoint) in headless Chromium and return its content."""
    global _PW_UNAVAILABLE
    if _PW_UNAVAILABLE:
        raise RuntimeError(_PW_UNAVAILABLE)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        _PW_UNAVAILABLE = f"playwright not installed ({e})"
        raise RuntimeError(_PW_UNAVAILABLE)

    fc = cfg.get("fetch", {})
    wait_ms = int(fc.get("playwright_wait_ms", 4000))
    ua = fc.get("user_agent", "Mozilla/5.0")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(user_agent=ua, locale="en-US",
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=wait_ms)
                else:
                    page.wait_for_timeout(wait_ms)
            except Exception:
                pass  # settle timeout is best-effort
            # For JSON endpoints the body is the raw JSON; for HTML it's the DOM.
            content = page.content()
            body_text = ""
            try:
                body_text = page.inner_text("body")
            except Exception:
                pass
            return content if len(content) > len(body_text) else (body_text or content)
        finally:
            browser.close()


# ------------------------------------------------------------------ public API
def fetch(source: dict, cfg: dict) -> tuple[str | None, dict]:
    """
    Fetch one source. Returns (payload_text_or_None, health_record).
    payload is raw text (HTML or JSON string) for parsers.py to interpret.
    """
    if not source.get("enabled", True):
        return None, _health(source, "disabled")

    method = source.get("method", "html")
    url = source.get("url", "")
    if method == "telegram":
        url = _cache_bust(url)

    # 1) plain HTTP first (fast, cheap) for everything except pure-JS hubs.
    if method != "playwright":
        try:
            r = _requests_get(url, cfg)
            if r.status_code in (403, 451):
                # geo/cloud block — try a real browser fingerprint.
                return _fallback_playwright(source, cfg, reason=f"HTTP {r.status_code}")
            if r.status_code >= 400:
                return _fallback_playwright(source, cfg, reason=f"HTTP {r.status_code}")
            text = r.text or ""
            if not text.strip():
                return _fallback_playwright(source, cfg, reason="empty body")
            h = _health(source, "ok")
            return text, h
        except (requests.ConnectionError, requests.Timeout) as e:
            return _fallback_playwright(source, cfg, reason=f"{type(e).__name__}")
        except Exception as e:  # noqa: BLE001
            return None, _health(source, "failed", str(e)[:200])

    # 2) playwright-native sources (JS SPAs)
    try:
        content = _playwright_get(url, cfg)
        if not content or not content.strip():
            return None, _health(source, "empty", "playwright empty content")
        if _looks_blocked(content):
            return None, _health(source, "blocked", "access-denied / challenge page")
        return content, _health(source, "ok")
    except Exception as e:  # noqa: BLE001
        return None, _health(source, "blocked", f"playwright: {str(e)[:160]}")


def _fallback_playwright(source: dict, cfg: dict, reason: str) -> tuple[str | None, dict]:
    """Second attempt via headless browser when HTTP was blocked/empty."""
    url = source.get("url", "")
    if source.get("method") == "telegram":
        url = _cache_bust(url)
    try:
        content = _playwright_get(url, cfg)
        if content and content.strip() and not _looks_blocked(content):
            h = _health(source, "ok")
            h["error"] = f"http failed ({reason}); recovered via playwright"
            return content, h
        if content and _looks_blocked(content):
            return None, _health(source, "blocked", f"{reason}; access-denied/challenge page")
        return None, _health(source, "blocked", f"{reason}; playwright empty")
    except Exception as e:  # noqa: BLE001
        # Distinguish an outright block from a generic failure.
        status = "blocked" if any(s in reason for s in ("403", "451", "ConnectionError", "Reset")) else "failed"
        return None, _health(source, status, f"{reason}; playwright: {str(e)[:140]}")


def fetch_detail(url: str, cfg: dict, referer: str | None = None) -> str | None:
    """
    Fetch a detail page and return its HTML/text. HTTP first, Playwright fallback.
    Used by Mode B (few pages, user-initiated) where completeness beats speed.
    """
    if not url:
        return None
    try:
        r = _requests_get(url, cfg, referer=referer)
        if r.status_code < 400 and r.text and r.text.strip():
            return r.text
    except Exception:
        pass
    try:
        return _playwright_get(url, cfg)
    except Exception:
        return None


def fetch_detail_fast(url: str, cfg: dict, timeout: float = 8.0) -> str | None:
    """
    FAST, best-effort detail fetch for Mode A date-hunting: a single HTTP GET with a
    short timeout, NO retries and NO Playwright. A blocked/slow page fails in ~1s
    instead of tying the run up for 45s. Pages we can't grab quickly just stay
    date_confidence:"unverified" (Mode B or the user can resolve them).
    """
    if not url:
        return None
    try:
        r = requests.get(url, headers=_headers(cfg), timeout=timeout)
        if r.status_code < 400 and r.text and r.text.strip() and not _looks_blocked(r.text):
            return r.text
    except Exception:
        return None
    return None


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""
