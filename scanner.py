#!/usr/bin/env python3
"""
scanner.py — main entry point. Modes A (free, pure code) and B (manual, AI).

CLI:
  python scanner.py --once                     one Mode-A scan (used by scan.yml)
  python scanner.py --smart --model haiku|sonnet   Mode-B enrichment (smart-scan.yml)
  python scanner.py --test-sources             fetch every source, print health, write nothing
  python scanner.py --dry-run                  full Mode-A scan to a temp dir; no seen/alerts

Mode A is strictly LLM-free. Mode B is the only path that uses the Claude API.
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import tempfile
import datetime as dt
from difflib import SequenceMatcher

import yaml

import fetchers
import parsers
import classify
import render
import notify
import smart

UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(d: dt.datetime) -> str:
    return d.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
#  IO helpers
# --------------------------------------------------------------------------- #
def load_yaml(name: str) -> dict:
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_text(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --------------------------------------------------------------------------- #
#  Fetch + parse one source
# --------------------------------------------------------------------------- #
def scan_source(source: dict, cfg: dict, now_iso: str):
    """Returns (records, health)."""
    payload, health = fetchers.fetch(source, cfg)
    if payload is None:
        return [], health

    try:
        candidates = parsers.parse_source(source, payload, cfg)
    except Exception as e:  # noqa: BLE001
        health["status"] = "failed"
        health["error"] = f"parse error: {str(e)[:160]}"
        return [], health

    records, newest = [], None
    for cand in candidates:
        try:
            rec = parsers.build_record(cand, source, cfg, now_iso)
        except Exception as e:  # noqa: BLE001
            print(f"[scan] build_record error ({source.get('name')}): {e}")
            continue
        if rec:
            records.append(rec)
        # track newest published date for freshness, from ALL candidates
        pub = cand.get("published_utc")
        if pub and (newest is None or pub > newest):
            newest = pub

    health["newest_item_date"] = newest
    health["kept"] = len(records)
    health["seen_raw"] = len(candidates)

    # freshness -> stale
    stale_days = source.get("stale_days", cfg.get("thresholds", {}).get("stale_days_default", 5))
    if health["status"] == "ok" and newest:
        try:
            age = (now_utc() - dt.datetime.fromisoformat(newest)).days
            if age > stale_days:
                health["status"] = "stale"
                health["error"] = f"newest item {age}d old (> {stale_days}d)"
        except Exception:
            pass
    if health["status"] == "ok" and not candidates:
        health["status"] = "empty"
    return records, health


# --------------------------------------------------------------------------- #
#  Dedup: official URL first, then fuzzy (venue + name)
# --------------------------------------------------------------------------- #
def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def dedup(records: list) -> list:
    kept, by_url = [], {}
    for r in records:
        cu = parsers.clean_url(r.get("official_link", ""))
        if cu and cu in by_url:
            _merge_dupe(by_url[cu], r)
            continue
        # fuzzy: same venue + very similar name
        dup = None
        for k in kept:
            if k["venue"] == r["venue"] and _fuzzy(k["name"], r["name"]) > 0.9:
                dup = k
                break
        if dup:
            _merge_dupe(dup, r)
            continue
        kept.append(r)
        if cu:
            by_url[cu] = r
    return kept


def _merge_dupe(keep: dict, other: dict):
    """Prefer confirmed dates / a real prize / native source over aggregator."""
    if keep.get("date_confidence") != "confirmed" and other.get("date_confidence") == "confirmed":
        keep["start_utc"] = other.get("start_utc")
        keep["end_utc"] = other.get("end_utc")
        keep["date_confidence"] = "confirmed"
    if not keep.get("prize") and other.get("prize"):
        keep["prize"] = other["prize"]
    if not keep.get("end_utc") and other.get("end_utc"):
        keep["end_utc"] = other["end_utc"]
        keep["start_utc"] = keep.get("start_utc") or other.get("start_utc")


# --------------------------------------------------------------------------- #
#  Mode-A free date extraction from detail pages (NEW items only, capped)
# --------------------------------------------------------------------------- #
def hunt_dates(records, cfg):
    """
    Open detail pages and regex-extract competition dates (fast HTTP only, capped).
    Runs EVERY scan for any record whose dates aren't confirmed — not just new ones —
    so a wrong first guess (e.g. article publish dates mistaken for the period)
    self-corrects on the next scan instead of sticking forever.
    """
    cap = int(cfg.get("thresholds", {}).get("detail_fetch_cap", 40))
    delay = float(cfg.get("thresholds", {}).get("detail_fetch_delay_sec", 1.5))
    budget = cap
    # date-less records first — they gain the most from a fetch
    ordered = sorted(records, key=lambda r: 0 if not r.get("end_utc") else 1)
    for r in ordered:
        if budget <= 0:
            break
        if r.get("tier") == "avoid":
            continue
        if r.get("date_confidence") == "confirmed" and r.get("end_utc"):
            continue
        html = fetchers.fetch_detail_fast(r.get("official_link", ""), cfg)  # fast HTTP-only
        budget -= 1
        time.sleep(delay)
        if not html:
            continue
        text = parsers._text(html) if "<" in html else html
        s, e, conf = parsers.extract_dates(text)
        if e:
            r["start_utc"] = s or r.get("start_utc")
            r["end_utc"] = e
            r["date_confidence"] = conf
        elif s and not r.get("start_utc"):
            r["start_utc"] = s
    return records


# --------------------------------------------------------------------------- #
#  Merge with previous data.json — protect Mode-B enrichment + retention
# --------------------------------------------------------------------------- #
def merge_and_retain(fresh: list, prev_data: dict, cfg: dict, now: dt.datetime):
    th = cfg.get("thresholds", {})
    retention = dt.timedelta(days=th.get("retention_days", 7))
    absent_drop = dt.timedelta(days=th.get("absent_drop_days", 7))
    now_iso = iso(now)

    prev = {c["id"]: c for c in prev_data.get("competitions", []) if c.get("id")}
    fresh_by_id = {}
    for r in fresh:
        r["last_seen_utc"] = now_iso
        p = prev.get(r["id"])
        if p:
            # carry the earliest first_seen
            r["first_seen_utc"] = p.get("first_seen_utc", r["first_seen_utc"])
            # PROTECT Mode-B enrichment: keep note + confirmed dates from previous
            if p.get("note"):
                r["note"] = p["note"]
            if p.get("date_confidence") == "confirmed" and r.get("date_confidence") != "confirmed":
                r["start_utc"] = p.get("start_utc")
                r["end_utc"] = p.get("end_utc")
                r["date_confidence"] = "confirmed"
            if p.get("smart_verdict"):
                r["smart_verdict"] = p["smart_verdict"]
        fresh_by_id[r["id"]] = r

    # retain previous items that dropped off this scan but shouldn't disappear yet
    for pid, p in prev.items():
        if pid in fresh_by_id:
            continue
        end = _parse_iso(p.get("end_utc"))
        last_seen = _parse_iso(p.get("last_seen_utc")) or _parse_iso(p.get("first_seen_utc")) or now
        keep = False
        if end and end > now:
            keep = True                                  # still live
        elif end and (now - end) <= retention:
            keep = True                                  # recently ended, grey card
        elif not end and (now - last_seen) <= absent_drop:
            keep = True                                  # unknown-date, seen recently
        if keep:
            # re-screen with the CURRENT filter: previously stored noise (e.g. World Cup
            # promos saved before a filter tightening) gets purged instead of lingering
            v = classify.classify_item(p.get("name", ""), "", cfg)
            if v["keep"]:
                fresh_by_id[pid] = p

    # drop ended-beyond-retention outright
    out = []
    for c in fresh_by_id.values():
        end = _parse_iso(c.get("end_utc"))
        if end and (now - end) > retention:
            continue
        out.append(c)
    return out


def _parse_iso(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Decorate records with live fields for dashboard/telegram
# --------------------------------------------------------------------------- #
def decorate(records, cfg, now: dt.datetime):
    soon = cfg.get("thresholds", {}).get("ending_soon_hours", 24)
    for c in records:
        end = _parse_iso(c.get("end_utc"))
        if end:
            hl = (end - now).total_seconds() / 3600.0
            c["hours_left"] = round(hl, 2)
            c["ended"] = hl <= 0
            c["ends_soon"] = 0 < hl <= soon
        else:
            c["hours_left"] = None
            c["ended"] = False
            c["ends_soon"] = False
    return records


# --------------------------------------------------------------------------- #
#  MODE A
# --------------------------------------------------------------------------- #
def run_once(cfg, sources, out_dir, dry_run=False):
    now = now_utc()
    now_iso = iso(now)
    data_path = os.path.join(out_dir, "data.json")
    seen_path = os.path.join(out_dir, "seen.json")
    index_path = os.path.join(out_dir, "index.html")

    prev_data = load_json(data_path, {})
    seen = load_json(seen_path, {})
    first_ever = len(seen) == 0

    all_records, health = [], []
    for src in sources:
        recs, h = scan_source(src, cfg, now_iso)
        all_records.extend(recs)
        health.append(h)
        print(f"[scan] {h['source']:<28} {h['status']:<8} kept={h.get('kept',0)} "
              f"raw={h.get('seen_raw',0)} newest={h.get('newest_item_date')}")

    all_records = dedup(all_records)

    # free date hunting (every scan, for anything without confirmed dates)
    all_records = hunt_dates(all_records, cfg)

    # merge with previous (protect Mode B) + retention
    merged = merge_and_retain(all_records, prev_data, cfg, now)

    # is_new via seen memory
    new_ids = []
    for c in merged:
        if c["id"] not in seen:
            c["is_new"] = True
            new_ids.append(c["id"])
            seen[c["id"]] = now_iso
        else:
            c["is_new"] = False

    merged = decorate(merged, cfg, now)
    merged.sort(key=lambda c: (c.get("hours_left") is None, c.get("hours_left") if c.get("hours_left") is not None else 1e9))

    data = {
        "generated_utc": now_iso,
        "last_mode": "A",
        "timezone": cfg.get("timezone", "Asia/Karachi"),
        "smart_configured": smart.is_configured(),
        "competitions": merged,
        "source_health": health,
    }

    if dry_run:
        write_json(data_path, data)
        write_text(index_path, render.render(data, cfg))
        print(f"\n[dry-run] wrote {data_path} and {index_path} ({len(merged)} comps). "
              f"seen.json NOT updated, no alerts sent.")
        return data

    # prune old seen entries
    seen = prune_seen(seen, cfg, now)
    write_json(seen_path, seen)
    write_json(data_path, data)
    write_text(index_path, render.render(data, cfg))

    # Telegram — suppress the flood on the very first run
    notify.send_scan_alert(data, cfg, new_ids, suppress_new=first_ever)
    print(f"\n[scan] done: {len(merged)} competitions, {len(new_ids)} new"
          f"{' (first run — alert suppressed)' if first_ever else ''}.")
    return data


def prune_seen(seen: dict, cfg, now: dt.datetime):
    days = cfg.get("thresholds", {}).get("seen_prune_days", 180)
    cutoff = now - dt.timedelta(days=days)
    out = {}
    for k, v in seen.items():
        d = _parse_iso(v)
        if d is None or d >= cutoff:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
#  MODE B
# --------------------------------------------------------------------------- #
def run_smart_mode(cfg, model_key, out_dir):
    now = now_utc()
    data_path = os.path.join(out_dir, "data.json")
    index_path = os.path.join(out_dir, "index.html")
    data = load_json(data_path, None)
    if not data:
        print("[smart] no data.json found — run a Mode-A scan first.")
        return None

    data, summary, n = smart.run_smart(data, cfg, model_key)
    data["competitions"] = decorate(data.get("competitions", []), cfg, now)
    data["generated_utc"] = iso(now)
    write_json(data_path, data)
    write_text(index_path, render.render(data, cfg))
    print(f"[smart] {summary}")
    if smart.is_configured():
        notify.send_text(f"🧠 Smart scan complete — {summary}. Dashboard updated.")
    return data


# --------------------------------------------------------------------------- #
#  --test-sources
# --------------------------------------------------------------------------- #
def test_sources(cfg, sources):
    now_iso = iso(now_utc())
    print(f"\n{'SOURCE':<30}{'STATUS':<10}{'KEPT':<6}{'RAW':<6}{'NEWEST ITEM':<22}ERROR")
    print("-" * 100)
    rows = []
    for src in sources:
        recs, h = scan_source(src, cfg, now_iso)
        rows.append(h)
        err = (h.get("error") or "")[:40]
        print(f"{h['source']:<30}{h['status']:<10}{h.get('kept',0):<6}"
              f"{h.get('seen_raw',0):<6}{str(h.get('newest_item_date'))[:19]:<22}{err}")
    ok = sum(1 for r in rows if r["status"] == "ok")
    stale = sum(1 for r in rows if r["status"] == "stale")
    bad = sum(1 for r in rows if r["status"] in ("blocked", "failed", "empty"))
    print("-" * 100)
    print(f"summary: {ok} ok, {stale} stale, {bad} blocked/failed/empty, of {len(rows)} sources")
    return rows


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Crypto Spot Competition Tracker")
    ap.add_argument("--once", action="store_true", help="one Mode-A scan (pure code)")
    ap.add_argument("--smart", action="store_true", help="Mode-B enrichment (uses Claude API)")
    ap.add_argument("--model", default="haiku", choices=["haiku", "sonnet"], help="Mode-B model")
    ap.add_argument("--test-sources", action="store_true", help="fetch all sources, print health, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="full Mode-A scan to temp dir; no seen/alerts")
    args = ap.parse_args()

    cfg = load_yaml("config.yaml")
    sources = [s for s in (load_yaml("sources.yaml").get("sources") or [])]
    enabled = [s for s in sources if s.get("enabled", True)]

    try:
        if args.test_sources:
            test_sources(cfg, enabled)
        elif args.smart:
            run_smart_mode(cfg, args.model, HERE)
        elif args.dry_run:
            tmp = os.path.join(tempfile.gettempdir(), "comp_tracker_dryrun")
            os.makedirs(tmp, exist_ok=True)
            # seed with the real data.json so retention/merge behave realistically
            real = os.path.join(HERE, "data.json")
            if os.path.exists(real):
                write_json(os.path.join(tmp, "data.json"), load_json(real, {}))
            run_once(cfg, enabled, tmp, dry_run=True)
        elif args.once:
            run_once(cfg, enabled, HERE, dry_run=False)
        else:
            ap.print_help()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        stage = "smart scan" if args.smart else "auto scan"
        try:
            notify.send_crash_alert(stage, e)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
