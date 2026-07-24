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
    end = _to_pkt(rec.get("end_utc"), cfg)
    warn = "" if rec.get("date_confidence") == "confirmed" or not rec.get("end_utc") else ""
    unv = "  ⚠ verify dates" if rec.get("date_confidence") != "confirmed" and rec.get("end_utc") else ""
    line = f"• <b>{name}</b>\n   {venue} · {prize} · ends {end}{unv}\n   <a href=\"{html.escape(link)}\">open</a>\n"
    return line


def send_scan_alert(data: dict, cfg: dict, new_ids: list[str], suppress_new: bool) -> bool:
    """
    Compose and send the post-scan alert.
    Returns True if something was sent (or nothing needed sending).
    """
    if not configured():
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping alert (scan still ran).")
        return False

    comps = [c for c in data.get("competitions", []) if c.get("tier") != "avoid"]
    live = [c for c in comps if not c.get("ended")]
    new_recs = [c for c in comps if c.get("id") in set(new_ids)] if not suppress_new else []
    soon_h = cfg.get("thresholds", {}).get("ending_soon_hours", 24)
    ending = [c for c in live if c.get("hours_left") is not None and 0 <= c["hours_left"] <= soon_h]

    health = data.get("source_health", [])
    problems = [h for h in health if h.get("status") in ("blocked", "failed", "stale")]

    ts = _to_pkt(data.get("generated_utc"), cfg)
    header = (f"🏆 <b>Comp Tracker</b> — {len(live)} live "
              f"(mode {data.get('last_mode', 'A')}) · {ts}\n\n")

    blocks = []
    if suppress_new:
        blocks.append("First run — dashboard populated. Future runs alert on NEW comps only.\n\n")
    elif new_recs:
        blocks.append("🆕 <b>New competitions</b>\n")
        blocks.extend(_fmt_comp(c, cfg) for c in new_recs)
        blocks.append("\n")

    if ending:
        blocks.append(f"⏰ <b>Ending within {soon_h}h</b>\n")
        blocks.extend(_fmt_comp(c, cfg) for c in ending)
        blocks.append("\n")

    if not new_recs and not ending and not suppress_new:
        blocks.append("No new comps and none ending soon. Dashboard is current.\n")

    if problems:
        plines = ", ".join(f"{html.escape(h['source'])} ({h['status']})" for h in problems[:12])
        blocks.append(f"\n🏥 <b>Source issues:</b> {plines}\n")

    url = (cfg.get("dashboard_url") or "").strip()
    footer = f"\n📊 <a href=\"{html.escape(url)}\">Open dashboard</a>" if url.startswith("http") else ""
    return _chunk_and_send(header, blocks, footer)


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
