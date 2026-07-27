"""
notify.py — Telegram alerts.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
If either is missing, print a warning and continue (never crash a scan).

Rules baked in per spec + decisions:
  * AVOID-tier venues are NEVER included in Telegram (dashboard only).
  * First-ever run (empty seen memory) does not blast every comp as "new".
  * Messages are chunked to stay under Telegram's 4096-char limit.
  * Only official_link URLs are sent.
"""
from __future__ import annotations

import os
import re
import time
import html
import datetime as dt

import requests

TG_LIMIT = 3800  # stay comfortably under 4096 after HTML entities


def _creds():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def configured() -> bool:
    tok, chat = _creds()
    return bool(tok and chat)


def _to_pkt(iso: str, cfg: dict) -> str:
    if not iso:
        return "date TBD"
    try:
        d = dt.datetime.fromisoformat(iso)
        pkt = d.astimezone(dt.timezone(dt.timedelta(hours=5)))
        return pkt.strftime("%d %b, %I:%M %p PKT").lstrip("0")
    except Exception:
        return "date TBD"


def _send_one(text: str) -> bool:
    tok, chat = _creds()
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=25)
        if r.status_code != 200:
            print(f"[notify] Telegram API {r.status_code}: {r.text[:180]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify] Telegram send failed: {e}")
        return False


def _chunk_and_send(header: str, blocks: list[str], footer: str = "") -> bool:
    """Pack blocks into <=TG_LIMIT messages, each carrying the header."""
    ok = True
    buf = header
    sent_any = False
    for b in blocks:
        if len(buf) + len(b) + len(footer) + 2 > TG_LIMIT:
            ok = _send_one(buf + footer) and ok
            sent_any = True
            time.sleep(0.5)
            buf = header + b
        else:
            buf += b
    if buf.strip():
        ok = _send_one(buf + footer) and ok
        sent_any = True
    return ok and sent_any


def _fmt_comp(rec: dict, cfg: dict) -> str:
    name = html.escape(rec.get("name", "")[:120])
    venue = html.escape(rec.get("venue", ""))
    prize = html.escape(rec.get("prize") or "prize TBD")
    link = rec.get("official_link", "")
    big = "🔥 " if prize_value(rec.get("prize")) >= float(
        cfg.get("thresholds", {}).get("telegram_big_prize_usd", 100000)) else ""

    # say plainly how long is left, then the exact end time
    hl = rec.get("hours_left")
    if rec.get("end_utc"):
        end = _to_pkt(rec.get("end_utc"), cfg)
        left = ""
        if isinstance(hl, (int, float)) and hl > 0:
            d, h = int(hl // 24), int(hl % 24)
            left = f"{d}d {h}h left · " if d else f"{h}h left · "
        unv = "  ⚠ verify dates" if rec.get("date_confidence") != "confirmed" else ""
        timing = f"{left}ends {end}{unv}"
    else:
        timing = "⚠ dates unknown — check the official page"

    return (f"• {big}<b>{name}</b>\n"
            f"   {venue} · {prize}\n"
            f"   {timing}\n"
            f"   <a href=\"{html.escape(link)}\">open</a>\n")


_PRIZE_NUM_RE = re.compile(r"([\d][\d,\.]*)\s*([km])?", re.I)


def prize_value(prize) -> float:
    """Rough numeric size of a prize string ('$300K' -> 300000) for ranking."""
    if not prize:
        return 0.0
    m = _PRIZE_NUM_RE.search(str(prize))
    if not m:
        return 0.0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    suf = (m.group(2) or "").lower()
    return n * (1_000 if suf == "k" else 1_000_000 if suf == "m" else 1)


def health_changes(health: list, prev_health: list) -> tuple[list, list]:
    """
    Which sources BROKE or RECOVERED since the previous scan.

    Reporting every currently-broken source on every run means permanently
    blocked ones (Gate, OKX) repeat twice a day forever until the line gets
    ignored — so only transitions are worth your attention.
    """
    bad = {"blocked", "failed", "stale", "empty"}
    prev = {h.get("source"): h.get("status") for h in (prev_health or [])}
    broke, fixed = [], []
    for h in health or []:
        name, now = h.get("source"), h.get("status")
        was = prev.get(name)
        if was is None:
            continue                      # brand-new source: not a change
        if now in bad and was not in bad:
            broke.append((name, now))
        elif now not in bad and was in bad:
            fixed.append(name)
    return broke, fixed


def send_scan_alert(data: dict, cfg: dict, new_ids: list[str], suppress_new: bool,
                    prev_health: list | None = None) -> bool:
    """
    Compose and send the post-scan alert.

    Sections are ordered by urgency and each competition appears ONCE, in the
    most urgent section that applies:
        ⏰ ending soon  ->  🆕 new  ->  ⏳ ending this week
    Returns True if something was sent (or nothing needed sending).
    """
    if not configured():
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping alert (scan still ran).")
        return False

    th = cfg.get("thresholds", {})
    soon_h = float(th.get("telegram_ending_hours", 48))
    horizon_h = float(th.get("telegram_horizon_days", 7)) * 24

    comps = [c for c in data.get("competitions", []) if c.get("tier") != "avoid"]
    live = [c for c in comps if not c.get("ended")]

    def hours(c):
        h = c.get("hours_left")
        return h if isinstance(h, (int, float)) else None

    by_prize = lambda lst: sorted(lst, key=lambda c: -prize_value(c.get("prize")))

    ending = by_prize([c for c in live if hours(c) is not None and 0 <= hours(c) <= soon_h])
    shown = {c.get("id") for c in ending}

    new_recs = []
    if not suppress_new:
        ids = set(new_ids)
        # only LIVE ones: a competition first seen after it already ended is not
        # news you can act on (it would read "ends 23 Jul" on a 27 Jul alert)
        new_recs = by_prize([c for c in live
                             if c.get("id") in ids and c.get("id") not in shown])
        shown |= {c.get("id") for c in new_recs}

    upcoming = by_prize([c for c in live
                         if c.get("id") not in shown
                         and hours(c) is not None and soon_h < hours(c) <= horizon_h])

    broke, fixed = health_changes(data.get("source_health", []), prev_health)

    ts = _to_pkt(data.get("generated_utc"), cfg)
    header = (f"🏆 <b>Comp Tracker</b> — {len(live)} live "
              f"(mode {data.get('last_mode', 'A')}) · {ts}\n\n")

    blocks = []
    if suppress_new:
        blocks.append("First run — dashboard populated. Future runs alert on NEW comps only.\n\n")

    if ending:
        blocks.append(f"⏰ <b>Ending within {int(soon_h)}h — act now</b>\n")
        blocks.extend(_fmt_comp(c, cfg) for c in ending)
        blocks.append("\n")

    if new_recs:
        blocks.append("🆕 <b>New competitions</b>\n")
        blocks.extend(_fmt_comp(c, cfg) for c in new_recs)
        blocks.append("\n")

    if upcoming:
        days = int(th.get("telegram_horizon_days", 7))
        blocks.append(f"⏳ <b>Ending within {days} days</b>\n")
        blocks.extend(_fmt_comp(c, cfg) for c in upcoming)
        blocks.append("\n")

    if not (ending or new_recs or upcoming or suppress_new):
        blocks.append(f"Nothing new, nothing ending within {days_word(th)}. "
                      f"{len(live)} competitions still running.\n")

    # only transitions — not the standing list of permanently blocked sources
    if broke:
        blocks.append("🏥 <b>Source stopped working:</b> "
                      + ", ".join(f"{html.escape(n)} ({html.escape(s)})" for n, s in broke[:8])
                      + "\n")
    if fixed:
        blocks.append("✅ <b>Source recovered:</b> "
                      + ", ".join(html.escape(n) for n in fixed[:8]) + "\n")

    url = (cfg.get("dashboard_url") or "").strip()
    footer = f"\n📊 <a href=\"{html.escape(url)}\">Open dashboard</a>" if url.startswith("http") else ""
    return _chunk_and_send(header, blocks, footer)


def days_word(th: dict) -> str:
    d = int(th.get("telegram_horizon_days", 7))
    return "a week" if d == 7 else f"{d} days"


def send_text(message: str) -> bool:
    """Generic sender (used for crash alerts and the smart-scan summary)."""
    if not configured():
        print(f"[notify] (Telegram not configured) would send: {message[:200]}")
        return False
    return _send_one(message[:TG_LIMIT])


def send_crash_alert(where: str, err: str) -> bool:
    msg = (f"⚠️ <b>Comp Tracker: scan crashed</b>\n"
           f"Stage: {html.escape(where)}\n"
           f"Error: {html.escape(str(err)[:400])}\n"
           f"The dashboard may be stale until the next run.")
    return send_text(msg)
