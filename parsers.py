"""
parsers.py — turn raw source payloads into normalized competition records.

Also holds:
  * extract_dates(): pure-code date finder used by Mode A. It only marks dates
    'confirmed' when the source text states a timezone (UTC / UTC+8 / SGT ...),
    because an 8-hour timezone guess on a "ends in 6 hours" alert is worse than
    honestly saying "verify".
  * stable_id(): the permanent per-competition key (cleaned official URL) that
    lets Mode A preserve Mode B enrichment across runs.

No network here. fetchers.py fetches; scanner.py orchestrates.
"""
from __future__ import annotations

import re
import json
import datetime as dt
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

import classify

UTC = dt.timezone.utc

# --------------------------------------------------------------------------- #
#  Stable identity
# --------------------------------------------------------------------------- #
_TRACK_PARAMS = ("utm_", "gclid", "fbclid", "ref", "_cb", "spm", "channel")


def clean_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        host = p.netloc.lower()
        path = p.path.rstrip("/")
        # drop tracking query params, keep meaningful ones
        return urlunparse((p.scheme or "https", host, path, "", "", ""))
    except Exception:
        return url.strip()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80]


def stable_id(url: str, venue: str, name: str) -> str:
    cu = clean_url(url)
    if cu and urlparse(cu).path not in ("", "/"):
        return cu
    return f"{slugify(venue)}::{slugify(name)}"


# --------------------------------------------------------------------------- #
#  Date extraction (timezone-aware, confirmed only when TZ stated)
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_NAMED_TZ = {
    "utc": 0, "gmt": 0, "z": 0, "sgt": 8, "hkt": 8, "cst": 8,  # china standard
    "kst": 9, "jst": 9, "ist": 5.5, "cet": 1, "eet": 2,
    "est": -5, "edt": -4, "pst": -8, "pdt": -7,
}

_TZ_OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-]\s*\d{1,2})(?::?(\d{2}))?", re.I)
_TZ_NAME_RE = re.compile(r"\b(utc|gmt|sgt|hkt|kst|jst|cet|eet|est|edt|pst|pdt)\b", re.I)

# ---- date token patterns -------------------------------------------------
# ISO-ish: 2026-07-28 08:00 | 2026/07/28T08:00
_ISO_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")
# Numeric US-style: 05/21/2026 (also accepts 21/05/2026 when first number > 12)
_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_MON_PAT = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
            r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# "July 22" — year/time (if any) found by scanning just after; (?!\d) stops "may 2026" -> day 20
_CORE_MDY = re.compile(r"\b" + _MON_PAT + r"\.?\s{0,3}(\d{1,2})(?:st|nd|rd|th)?(?!\d)", re.I)
# "22 July [2026]"
_CORE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?(?!\d)\s{0,3}" + _MON_PAT + r"\b\.?", re.I)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# ---- context patterns ----------------------------------------------------
# Dates right after these words are ARTICLE metadata, not competition dates.
_META_BACK = re.compile(r"(publish|updated|posted|modified|edited|released)", re.I)
# Words that mark a date as part of the event period.
_PERIOD_HINT = re.compile(
    r"(period|phase|runs?|running|from|start|begin|launch|end|until|till|through|"
    r"between|duration|campaign|event|deadline|clos\w*|expir\w*|live|held)", re.I)
# A lone date preceded by these is an END date.
_END_BACK = re.compile(
    r"(end(?:s|ed|ing)?(?:\s+on)?|until|till|through|deadline|clos(?:es|ing|ed)?|expir\w*)"
    r"[\s:\-–]{0,4}$", re.I)
# Connector between two dates of a range: "– | to | until | and", possibly after a tz.
_GAP_RANGE = re.compile(
    r"^[\s,()]*(?:(?:utc|gmt|sgt|hkt|kst|jst|cet|eet|est|edt|pst|pdt)\b[\s,()]*)?"
    r"(?:[+\-]\s?\d{1,2}(?::?\d{2})?[\s,()]*)?"
    r"(?:(?:to|until|till|through|and)\b|[–—−~→➡>-])[\s,()]*$", re.I)
# Chars allowed between a date core and its own year token (time digits, tz words).
_CLEAN_GAP = re.compile(r"^[\s\d:.,()]*(?:(?:utc|gmt|sgt|hkt|kst|jst|am|pm)\b[\s\d:.,()+\-]*)*$",
                        re.I)


def _detect_offset(text: str):
    """Return (offset_hours, tz_label) if a timezone is stated, else (None, None)."""
    m = _TZ_OFFSET_RE.search(text)
    if m:
        hrs = int(m.group(1).replace(" ", ""))
        mins = int(m.group(2)) if m.group(2) else 0
        lbl = f"UTC{hrs:+d}" + (f":{mins:02d}" if mins else "")
        return hrs + (mins / 60.0) * (1 if hrs >= 0 else -1), lbl
    m = _TZ_NAME_RE.search(text)
    if m:
        name = m.group(1).lower()
        return _NAMED_TZ.get(name, 0), name.upper()
    return None, None


def _mk(y, mo, d, hh, mm):
    try:
        return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
    except Exception:
        return None


def _mk_infer(y, mo, d, hh, mm, now, had_year):
    """Build a datetime; if the year is missing, pick the year closest to now."""
    if had_year:
        d0 = _mk(y, mo, d, hh, mm)
        return d0 if d0 and 2020 <= d0.year <= 2035 else None
    best = None
    for yy in (now.year - 1, now.year, now.year + 1):
        d0 = _mk(yy, mo, d, hh, mm)
        if d0 and (best is None
                   or abs((d0 - now).total_seconds()) < abs((best - now).total_seconds())):
            best = d0
    return best


def _scan_tail(text, endpos):
    """
    After a "Month Day" core, look just ahead for a time (10:00) and/or a year (2026)
    that belong to THIS date — i.e. nothing but time digits / tz words in between.
    Returns (year|None, hh, mm, span_end).
    """
    tail = text[endpos:endpos + 34]
    span = endpos
    hh = mm = None
    yr = None
    tm = _TIME_RE.search(tail)
    if tm and re.fullmatch(r"[\s,(@]*(?:at\s*)?", tail[:tm.start()], re.I):
        hh, mm = tm.group(1), tm.group(2)
        span = endpos + tm.end()
    ym = _YEAR_RE.search(tail)
    if ym and _CLEAN_GAP.match(tail[:ym.start()]):
        yr = int(ym.group(1))
        span = max(span, endpos + ym.end())
        if hh is None:  # "28 July 2026 08:00" — time after the year
            after = text[endpos + ym.end():endpos + ym.end() + 12]
            t2 = _TIME_RE.search(after)
            if t2 and re.fullmatch(r"[\s,(]*", after[:t2.start()]):
                hh, mm = t2.group(1), t2.group(2)
                span = endpos + ym.end() + t2.end()
    return yr, hh, mm, span


def _collect_candidates(text, now):
    """Find every date mention with its text span. Returns sorted, de-overlapped list."""
    cands = []

    def add(pos, endpos, y, mo, d, hh, mm, had_year):
        d0 = _mk_infer(y, mo, d, hh, mm, now, had_year)
        if d0:
            cands.append({"pos": pos, "end": endpos, "dt": d0})

    for m in _ISO_RE.finditer(text):
        y, mo, d, hh, mm = m.groups()
        add(m.start(), m.end(), y, mo, d, hh, mm, True)
    for m in _NUM_RE.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mo, d = (a, b) if a <= 12 else (b, a)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            add(m.start(), m.end(), y, mo, d, None, None, True)
    for m in _CORE_MDY.finditer(text):
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            yr, hh, mm, span = _scan_tail(text, m.end())
            add(m.start(), span, yr, mo, m.group(2), hh, mm, yr is not None)
    for m in _CORE_DMY.finditer(text):
        mo = _MONTHS.get(m.group(2)[:3].lower())
        if mo:
            yr, hh, mm, span = _scan_tail(text, m.end())
            add(m.start(), span, yr, mo, m.group(1), hh, mm, yr is not None)

    cands.sort(key=lambda c: (c["pos"], -(c["end"] - c["pos"])))
    out, last_end = [], -1
    for c in cands:
        if c["pos"] < last_end:
            continue  # overlaps a date already taken
        out.append(c)
        last_end = c["end"]
    return out


def extract_dates(text: str):
    """
    Competition start/end extraction — context-aware.

    Rules (learned from real failures like Trust Wallet's Ondo comp, where
    'Published on: Jul 10 / Updated on: Jul 17' was mistaken for the period):
      * dates right after publish/update words are article METADATA -> ignored;
      * ranged pairs ("July 10 10:00 UTC – July 22", "Jul 17 - Jul 29") are the
        strongest signal; multi-phase events use earliest start -> latest end;
      * a lone date after "ends/ended on/until/deadline" is an END date;
      * year-less dates ("Jul 17") get the year closest to today;
      * numeric 05/21/2026 dates are understood;
      * confidence is 'confirmed' ONLY if the page states a timezone AND an end
        date was found.
    Returns (start_utc_iso|None, end_utc_iso|None, confidence).
    """
    if not text:
        return None, None, "unverified"
    text = re.sub(r"\s+", " ", text)[:8000]
    now = dt.datetime.now(UTC).replace(tzinfo=None)
    offset, _label = _detect_offset(text)
    cands = _collect_candidates(text, now)
    if not cands:
        return None, None, "unverified"

    # classify each date by its surrounding words
    for c in cands:
        back = text[max(0, c["pos"] - 40):c["pos"]]
        fwd = text[c["end"]:c["end"] + 12]
        c["meta"] = bool(_META_BACK.search(back[-32:])) and not _PERIOD_HINT.search(back[-22:])
        c["endh"] = bool(_END_BACK.search(back))
        c["period"] = bool(_PERIOD_HINT.search(back) or _PERIOD_HINT.search(fwd))
        c["ranged"] = False
    for i in range(len(cands) - 1):
        gap = text[cands[i]["end"]:cands[i + 1]["pos"]]
        if len(gap) <= 32 and _GAP_RANGE.match(gap):
            cands[i]["ranged"] = cands[i + 1]["ranged"] = True

    # keep plausible, non-metadata dates. The lower bound is deliberately WIDE
    # (420d): old stated dates must stay readable so long-ended archive events
    # get a past end_utc and retention removes them — a tight bound made them
    # resurrect forever as "dates TBD" zombies.
    lo, hi = now - dt.timedelta(days=420), now + dt.timedelta(days=400)
    usable = [c for c in cands if not c["meta"] and lo <= c["dt"] <= hi]
    if not usable:
        return None, None, "unverified"

    # strongest evidence first: ranges > hinted singles > anything left
    ranged = [c for c in usable if c["ranged"]]
    hinted = [c for c in usable if c["period"] or c["endh"]]
    pool = ranged or hinted or usable

    dts = sorted({c["dt"] for c in pool})
    if len(dts) >= 2:
        start, end = dts[0], dts[-1]
    else:
        only = dts[0]
        if any(c["endh"] for c in pool if c["dt"] == only):
            start, end = None, only
        else:
            start, end = only, None

    def to_utc(naive):
        if naive is None:
            return None
        off = offset if offset is not None else 0.0
        as_utc = naive - dt.timedelta(hours=off)
        return as_utc.replace(tzinfo=UTC).replace(microsecond=0).isoformat()

    conf = "confirmed" if (offset is not None and end is not None) else "unverified"
    return to_utc(start), to_utc(end), conf


# --------------------------------------------------------------------------- #
#  HTML helpers
# --------------------------------------------------------------------------- #
def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _text(html: str) -> str:
    return _soup(html).get_text(" ", strip=True)


def _anchor_candidates(html, base_url, href_needles, cfg, min_len=15):
    """Generic: every <a> whose href contains any needle and whose text looks like a title."""
    soup = _soup(html)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(n in href for n in href_needles):
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < min_len:
            continue
        url = urljoin(base_url, href)
        key = (clean_url(url), title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": title, "url": url, "body": title, "prize": None, "published_utc": None})
    return out


_PRIZE_RE = re.compile(
    r"(\$?\s?[\d][\d,\.]*\s?(?:k|m)?\s?(?:usdt|usdc|usd|bnb|eth|btc|sol|dollars?)\b|\$\s?[\d][\d,\.]*\s?(?:k|m)?)",
    re.I)

# Fallback: big number + arbitrary UPPERCASE token symbol ("200,000 AUC",
# "5,000,000 KITE", "100,000 $ZAMA"). Requires a comma or 4+ digits so tiny
# figures ("3-in-1", "24 JUL") don't match, and excludes month/word noise.
_PRIZE_TOKEN_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,})\s?\$?([A-Z][A-Z0-9]{1,7})\b")
_PRIZE_TOKEN_STOP = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                     "NOV", "DEC", "UTC", "GMT", "SGT", "HKT", "KST", "JST", "PKT", "AM",
                     "PM", "APR", "USD", "IN", "TO", "OF", "THE", "AND", "FOR", "WIN"}


def _guess_prize(text: str):
    m = _PRIZE_RE.search(text or "")
    if m:
        return m.group(0).strip()
    for tm in _PRIZE_TOKEN_RE.finditer(text or ""):
        if tm.group(2) not in _PRIZE_TOKEN_STOP:
            return (tm.group(1) + " " + tm.group(2)).strip()
    return None


# --------------------------------------------------------------------------- #
#  Per-source extractors  ->  list of candidate dicts
#  candidate = {name, url, body, prize, published_utc, [start_utc,end_utc,date_confidence], [venue]}
# --------------------------------------------------------------------------- #
def _iso_from_ms(ms):
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, UTC).replace(microsecond=0).isoformat()
    except Exception:
        return None


def _iso_from_any(ts):
    """Accept ms epoch, second epoch, or a naive datetime string (assumed UTC)."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
        n = int(ts)
        if n > 1e12:            # milliseconds
            return _iso_from_ms(n)
        if n > 1e9:             # seconds
            return dt.datetime.fromtimestamp(n, UTC).replace(microsecond=0).isoformat()
        return None
    if isinstance(ts, str):
        try:
            d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            return d.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            return None
    return None


def _extract_binance(payload, source, cfg):
    out = []
    try:
        data = json.loads(payload)
    except Exception:
        # payload may be page-wrapped JSON from Playwright fallback
        m = re.search(r"\{.*\}", payload or "", re.S)
        if not m:
            return out
        data = json.loads(m.group(0))
    cats = (((data or {}).get("data") or {}).get("catalogs")) or []
    arts = []
    for c in cats:
        arts.extend(c.get("articles") or [])
    if not arts:
        arts = (((data or {}).get("data") or {}).get("articles")) or []
    for a in arts:
        code = a.get("code") or a.get("id")
        url = f"https://www.binance.com/en/support/announcement/{code}" if code else "https://www.binance.com/en/support/announcement"
        out.append({
            "name": a.get("title", "").strip(),
            "url": url,
            "body": a.get("title", ""),
            "prize": _guess_prize(a.get("title", "")),
            "published_utc": _iso_from_ms(a.get("releaseDate")),
        })
    return out


def _extract_bybit(payload, source, cfg):
    out = []
    try:
        data = json.loads(payload)
    except Exception:
        m = re.search(r"\{.*\}", payload or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    lst = (((data or {}).get("result") or {}).get("list")) or []
    for a in lst:
        title = (a.get("title") or "").strip()
        start = _iso_from_ms(a.get("startDateTimestamp"))
        end = _iso_from_ms(a.get("endDateTimestamp"))
        cand = {
            "name": title,
            "url": a.get("url", ""),
            "body": f"{title}. {a.get('description', '')} [{a.get('type', {}).get('title', '')}]",
            "prize": _guess_prize(title + " " + (a.get("description") or "")),
            "published_utc": _iso_from_ms(a.get("dateTimestamp") or a.get("publishTime")),
        }
        if start:  # Bybit gives real UTC comp dates
            cand.update({"start_utc": start, "end_utc": end,
                         "date_confidence": "confirmed"})
        out.append(cand)
    return out


def _extract_bybit_html(payload, source, cfg):
    """
    announcements.bybit.com scrape — recovery path for the datacenter-blocked API.
    The site is Next.js: walk its __NEXT_DATA__ JSON for article objects, which can
    carry the SAME startDateTimestamp/endDateTimestamp fields as the old API
    (=> confirmed dates). Falls back to /article/ anchor scraping.
    """
    out = []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', payload or "", re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
        except Exception:
            nd = None
        if nd:
            def walk(node):
                if isinstance(node, dict):
                    title = node.get("title")
                    url = node.get("url") or node.get("route")
                    if (isinstance(title, str) and len(title.strip()) > 8
                            and isinstance(url, str) and "article" in url):
                        full = url if url.startswith("http") else urljoin(
                            "https://announcements.bybit.com", url)
                        desc = node.get("description") or ""
                        cand = {
                            "name": title.strip(),
                            "url": full,
                            "body": f"{title}. {desc}",
                            "prize": _guess_prize(title + " " + str(desc)),
                            "published_utc": _iso_from_any(
                                node.get("dateTimestamp") or node.get("publishTime")),
                        }
                        s = _iso_from_any(node.get("startDateTimestamp"))
                        e = _iso_from_any(node.get("endDateTimestamp"))
                        if s or e:
                            cand.update({"start_utc": s, "end_utc": e,
                                         "date_confidence": "confirmed"})
                        out.append(cand)
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(nd)
    if not out:
        out = _anchor_candidates(payload, "https://announcements.bybit.com",
                                 ["/article"], cfg)
    # de-dup by url
    seen, uniq = set(), []
    for c in out:
        k = clean_url(c.get("url", ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def _extract_kucoin_json(payload, source, cfg):
    out = []
    try:
        data = json.loads(payload)
    except Exception:
        m = re.search(r"\{.*\}", payload or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    items = (data.get("items") or (data.get("data") or {}).get("items")
             or (data.get("data") or {}).get("list") or data.get("list") or [])
    for it in items:
        path = (it.get("path") or it.get("url") or "").strip()
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            url = "https://www.kucoin.com/announcement" + path
        else:
            url = "https://www.kucoin.com/announcement/" + path
        title = (it.get("title") or "").strip()
        ts = (it.get("publish_at") or it.get("first_publish_at")
              or it.get("publish_ts") or it.get("publishTime") or it.get("cTime"))
        out.append({
            "name": title,
            "url": url,
            "body": f"{title}. {it.get('summary', '')}",
            "prize": _guess_prize(title + " " + (it.get("summary") or "")),
            "published_utc": _iso_from_any(ts),
        })
    return out


def _extract_kucoin_web3(payload, source, cfg):
    # Try __NEXT_DATA__ first, then fall back to anchors.
    out = []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', payload or "", re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
            blob = json.dumps(nd)
            for am in re.finditer(r'"title":"([^"]{12,140})".*?"(?:path|url|slug)":"([^"]+)"', blob):
                title, path = am.group(1), am.group(2)
                url = path if path.startswith("http") else urljoin("https://www.kucoin.com/web3/", path)
                out.append({"name": title, "url": url, "body": title,
                            "prize": _guess_prize(title), "published_utc": None})
        except Exception:
            pass
    if not out:
        out = _anchor_candidates(payload, "https://www.kucoin.com",
                                 ["/web3/support/articles/", "/web3/"], cfg)
    return out


def _extract_telegram(payload, source, cfg):
    soup = _soup(payload)
    out = []
    for wrap in soup.select(".tgme_widget_message_wrap, .tgme_widget_message"):
        txt_el = wrap.select_one(".tgme_widget_message_text")
        text = txt_el.get_text(" ", strip=True) if txt_el else ""
        if not text:
            continue
        # prefer an external link inside the message, else the message permalink
        link = ""
        for a in wrap.select(".tgme_widget_message_text a[href]"):
            href = a["href"]
            if "t.me" not in href:
                link = href
                break
        if not link:
            pl = wrap.select_one("a.tgme_widget_message_date[href]")
            link = pl["href"] if pl else source.get("url", "")
        t_el = wrap.select_one("time[datetime]")
        pub = None
        if t_el and t_el.get("datetime"):
            try:
                pub = dt.datetime.fromisoformat(t_el["datetime"].replace("Z", "+00:00")) \
                    .astimezone(UTC).replace(microsecond=0).isoformat()
            except Exception:
                pub = None
        out.append({"name": text[:120], "url": link, "body": text,
                    "prize": _guess_prize(text), "published_utc": pub})
    return out


# logo alt-text values on trading-tournaments cards that are NOT exchange names
_TT_ALT_IGNORE = {"trading-tournaments.com", "tournaments", "traders", "reviews",
                  "trading tournaments"}

# The aggregator also lists forex / prop-firm contests. Keep a card only if its venue
# is a KNOWN crypto exchange, or the card text shows a crypto signal.
_CRYPTO_SIGNALS = ("usdt", "usdc", "crypto", "token", "web3", "swap", "btc", "eth",
                   " bnb", " sol", "memecoin", "defi", "airdrop", "altcoin",
                   "on-chain", "onchain", "stablecoin")


def _known_venue_set(cfg):
    s = set()
    for names in (cfg.get("tiers") or {}).values():
        s |= {n.lower() for n in names}
    return s


def _extract_aggregator(payload, source, cfg):
    """
    trading-tournaments.com — one record per CARD.
    The exchange name is the card's logo <img alt> (reliable); title/link come from
    the /tournaments/{slug} anchor; prize/dates from the card text. Falls back to the
    old anchor scan if the card layout changes.
    """
    soup = _soup(payload)
    base = source.get("url", "")
    cards = soup.select("div.group.relative.h-full")
    out = []
    for card in cards:
        # venue from the logo alt-text
        venue = None
        for img in card.find_all("img"):
            alt = (img.get("alt") or "").strip()
            if alt and alt.lower() not in _TT_ALT_IGNORE:
                venue = alt
                break
        # title + link from the /tournaments/{slug} anchor (skip the "View details" repeat)
        title, link = None, None
        for anc in card.find_all("a", href=True):
            href = anc["href"]
            txt = anc.get_text(" ", strip=True)
            if href.startswith("/tournaments/") and len(txt) > 8 and "view details" not in txt.lower():
                title, link = txt, urljoin(base, href)
                break
        if not link:  # fall back to the /go/ redirect link
            for anc in card.find_all("a", href=True):
                if anc["href"].startswith(("/go/", "/tournaments/")):
                    link = urljoin(base, anc["href"])
                    break
        ctext = card.get_text(" ", strip=True)[:700]
        low = ctext.lower()
        if "futures" in low and "spot" not in low:
            continue  # skip futures-only cards
        # drop forex / prop-firm noise: unknown venue AND no crypto signal
        canon = classify.normalize_venue(venue or "", cfg).lower()
        if canon not in _known_venue_set(cfg) and not any(k in low for k in _CRYPTO_SIGNALS):
            continue
        if not title:
            title = (venue or "competition") + " tournament"
        s, e, conf = extract_dates(ctext)
        out.append({
            "name": title[:140],
            "url": link or urljoin(base, "/tournaments"),
            "body": ctext,
            "prize": _guess_prize(ctext),
            "published_utc": None,
            "venue": venue or "Unknown",
            "start_utc": s, "end_utc": e, "date_confidence": conf,
        })
    if not out:
        out = _extract_aggregator_fallback(payload, source, cfg)
    # de-dup by (venue,name)
    seen, uniq = set(), []
    for c in out:
        k = (c.get("venue"), c["name"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def _extract_aggregator_fallback(payload, source, cfg):
    """Old anchor-based scan, used only if the card layout changes."""
    soup = _soup(payload)
    out = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        if len(title) < 8 or "/tournaments/" not in a["href"]:
            continue
        container = a.find_parent(["article", "li", "div"]) or a
        ctext = container.get_text(" ", strip=True)[:700]
        low = ctext.lower()
        if "futures" in low and "spot" not in low:
            continue
        s, e, conf = extract_dates(ctext)
        out.append({
            "name": title[:140],
            "url": urljoin(source.get("url", ""), a["href"]),
            "body": ctext, "prize": _guess_prize(ctext), "published_utc": None,
            "venue": _aggregator_venue(ctext, cfg),
            "start_utc": s, "end_utc": e, "date_confidence": conf,
        })
    return out


def _aggregator_venue(text, cfg):
    low = text.lower()
    known = []
    for names in (cfg.get("tiers") or {}).values():
        known.extend(names)
    for v in sorted(known, key=len, reverse=True):
        if v.lower() in low:
            return v
    return "Unknown"


# Generic HTML sources -> anchor scraping with source-specific href filters
_HTML_NEEDLES = {
    "Gate": (["/announcements/article/"], "https://www.gate.com"),
    "OKX": (["/help/article/", "/announcements/"], "https://www.okx.com"),
    "Bitget": (["/support/articles/", "/support/article/"], "https://www.bitget.com"),
    "Trust Wallet": (["/blog/"], "https://trustwallet.com"),
    "OKX Web3 Wallet": (["/boost/trading-competition/", "/boost/x-campaign", "/campaign"], "https://web3.okx.com"),
    "Coin98": (["/blog/"], "https://coin98.com"),
    "1inch Wallet": (["/blog/"], "https://1inch.com"),
}


def parse_source(source: dict, payload: str, cfg: dict):
    """Dispatch to the right extractor; returns raw candidate dicts."""
    if not payload:
        return []
    method = source.get("method")
    venue = source.get("venue")
    if method == "bybit_api":
        return _extract_bybit(payload, source, cfg)
    if venue == "Bybit":
        return _extract_bybit_html(payload, source, cfg)
    if venue == "Binance":
        return _extract_binance(payload, source, cfg)
    if venue == "KuCoin" and method == "json_api":
        return _extract_kucoin_json(payload, source, cfg)
    if venue == "KuCoin Web3 Wallet":
        return _extract_kucoin_web3(payload, source, cfg)
    if method == "telegram":
        return _extract_telegram(payload, source, cfg)
    if venue == "aggregator":
        return _extract_aggregator(payload, source, cfg)
    if venue in _HTML_NEEDLES:
        needles, base = _HTML_NEEDLES[venue]
        return _anchor_candidates(payload, base, needles, cfg)
    # default: generic anchor scrape
    return _anchor_candidates(payload, source.get("url", ""), ["/article", "/blog", "/campaign"], cfg)


# --------------------------------------------------------------------------- #
#  Build normalized records (classify + dates + identity)
# --------------------------------------------------------------------------- #
def build_record(cand: dict, source: dict, cfg: dict, now_iso: str):
    name = (cand.get("name") or "").strip()
    if not name:
        return None
    body = cand.get("body") or name
    verdict = classify.classify_item(name, body, cfg)
    if not verdict["keep"]:
        return None

    venue = classify.normalize_venue(cand.get("venue") or source.get("venue", ""), cfg)
    url = cand.get("url") or source.get("url", "")

    # dates: use ones supplied by the extractor (e.g. Bybit confirmed), else parse text
    if cand.get("start_utc") or cand.get("date_confidence") == "confirmed":
        start_utc = cand.get("start_utc")
        end_utc = cand.get("end_utc")
        conf = cand.get("date_confidence", "unverified")
    else:
        start_utc, end_utc, conf = extract_dates(body)

    rec = {
        "id": stable_id(url, venue, name),
        "name": name,
        "venue": venue,
        "type": verdict["type"],
        "prize": cand.get("prize"),
        "start_utc": start_utc,
        "end_utc": end_utc,
        "date_confidence": conf,
        "structure": None,
        "entry": None,
        "eligibility": None,
        "fee": None,
        "official_link": url,
        "source": source.get("name", ""),
        "first_seen_utc": now_iso,
        "note": None,                     # Mode B only
        "tier": classify.venue_tier(venue, cfg),
        "classify_reason": verdict["reason"],
        "published_utc": cand.get("published_utc"),
    }
    return rec
