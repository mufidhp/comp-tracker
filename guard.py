#!/usr/bin/env python3
"""
guard.py — the filter's safety test, run before every scan publishes.

Every futures/derivative leak that ever reached the dashboard, every genuine
spot comp a filter change once wrongly dropped, and every page wording that once
produced wrong dates, is recorded in regression_cases.yaml. scanner.run_once() calls assert_filter_ok() first; if
any case gives the wrong answer the scan raises, Telegram gets the crash
alert, and the commit step never runs — so a broken filter can't quietly
refill the dashboard with futures comps.

Run by hand:  python guard.py
"""
from __future__ import annotations

import os
import sys
import datetime as dt

import yaml

import classify
import parsers

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(HERE, "regression_cases.yaml")


def run_cases(cfg: dict, cases: dict | None = None) -> tuple[int, list[str]]:
    """Return (number of cases checked, list of failure descriptions)."""
    if cases is None:
        with open(CASES_FILE, "r", encoding="utf-8") as f:
            cases = yaml.safe_load(f) or {}
    failures, n = [], 0

    for c in cases.get("title_cases") or []:
        n += 1
        v = classify.classify_item(c["title"], c.get("body") or c["title"], cfg,
                                   link=c.get("link") or "")
        want_keep = c["expect"] == "keep"
        if v["keep"] != want_keep:
            failures.append(f"{'KEPT' if v['keep'] else 'DROPPED'} but expected "
                            f"{c['expect']}: \"{c['title'][:70]}\" ({v['reason']})")
        elif want_keep and c.get("type") and v["type"] != c["type"]:
            failures.append(f"type {v['type']} but expected {c['type']}: \"{c['title'][:70]}\"")

    for c in cases.get("date_cases") or []:
        n += 1
        anchor = None
        if c.get("anchor"):
            anchor = dt.datetime.fromisoformat(str(c["anchor"]))
        _s, end, conf = parsers.extract_dates(c["text"], anchor=anchor)
        if c.get("end") and (end or "")[:16] != c["end"]:
            failures.append(f"date: end {end} but expected {c['end']}: \"{c['text'][:60]}\"")
        elif c.get("conf") and conf != c["conf"]:
            failures.append(f"date: confidence {conf} but expected {c['conf']}: \"{c['text'][:60]}\"")

    for c in cases.get("page_cases") or []:
        n += 1
        text = parsers.article_text(c["html"])
        if c.get("min_length") and len(text) < int(c["min_length"]):
            failures.append(f"page text unreadable ({len(text)} chars): {c['name']}")
            continue
        found = bool(classify.derivative_page_evidence(text, cfg))
        if found != bool(c["expect_futures"]):
            failures.append(f"page check said {'FUTURES' if found else 'clean'} but expected "
                            f"{'futures' if c['expect_futures'] else 'clean'}: {c['name']}")
    return n, failures


def assert_filter_ok(cfg: dict) -> None:
    n, failures = run_cases(cfg)
    if failures:
        raise RuntimeError(
            f"Filter safety test failed — {len(failures)} of {n} known cases wrong; "
            f"not publishing. First: {failures[0]}")
    print(f"[guard] filter safety test passed ({n} known cases)")


if __name__ == "__main__":
    with open(os.path.join(HERE, "config.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    n, failures = run_cases(cfg)
    for msg in failures:
        print("FAIL", msg)
    print(f"{n - len(failures)} of {n} cases pass")
    sys.exit(1 if failures else 0)
