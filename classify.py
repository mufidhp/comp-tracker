"""
classify.py — pure rule-based filtering and legitimacy tiers.

Two jobs:
  1. Decide whether a scraped item is a spot/onchain trading competition we keep,
     and tag it spot / onchain / mixed.
  2. Map a venue name to a safety tier (A / caution / avoid).

No network, no AI. Used by Mode A and (for the hard-coded AVOID guard) Mode B.
"""
from __future__ import annotations

import re

_PUNCT = str.maketrans({c: " " for c in "-_/|:;,.!?()[]{}\"'#*&"})


def _norm_text(*parts: str) -> str:
    """Lowercase + strip punctuation so keyword matching is robust."""
    joined = " ".join(p for p in parts if p)
    return " ".join(joined.lower().translate(_PUNCT).split())


def normalize_venue(name: str, cfg: dict) -> str:
    """Map a scraped/aggregator venue spelling to a canonical venue name."""
    if not name:
        return ""
    raw = name.strip()
    key = " ".join(raw.lower().translate(_PUNCT).split())
    # normalize alias keys the SAME way (strip punctuation) so "Gate.io" -> "gate io" matches
    aliases = {" ".join(k.lower().translate(_PUNCT).split()): v
               for k, v in (cfg.get("venue_aliases") or {}).items()}
    if key in aliases:
        return aliases[key]
    # Try to match a known tier name case-insensitively.
    for tier_names in (cfg.get("tiers") or {}).values():
        for canon in tier_names:
            if key == canon.lower():
                return canon
    return raw  # unknown venue kept as-is (treated as caution downstream)


def venue_tier(venue: str, cfg: dict) -> str:
    """Return 'A', 'caution', or 'avoid' for a venue. Unknown -> 'caution'."""
    tiers = cfg.get("tiers") or {}
    canon = normalize_venue(venue, cfg)
    low = canon.lower()
    for name in tiers.get("avoid", []):
        if low == name.lower():
            return "avoid"
    for name in tiers.get("A", []):
        if low == name.lower():
            return "A"
    for name in tiers.get("caution", []):
        if low == name.lower():
            return "caution"
    return "caution"


def is_avoid(venue: str, cfg: dict) -> bool:
    return venue_tier(venue, cfg) == "avoid"


# Keyword matching must respect WORD boundaries. Plain substring matching made
# short blockers eat innocent words — "ama" (meant for AMA sessions) killed
# "Amazing", "Panama" and the token "Zama"; "stake" killed "Sweepstakes"; "spot"
# matched "Spotlight". Those competitions vanished with no trace. A trailing "s"
# is still allowed so "cfd"/"giveaway"/"loan" keep catching their plurals.
_WORD_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _word_search(needle: str, haystack: str) -> bool:
    rx = _WORD_RE_CACHE.get(needle)
    if rx is None:
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(needle) + r"s?(?![a-z0-9])")
        _WORD_RE_CACHE[needle] = rx
    return bool(rx.search(haystack))


def _slug_words(link: str) -> str:
    """
    The last path segment of a link, split into words: ".../9thAnniversaryFuturesArena"
    -> "9th anniversary futures arena". Venues often name the event honestly in the
    URL while the title stays generic ("Trade to Win VIP Golf Bag"). Numeric ids
    ("12560603897906") contribute nothing.
    """
    if not link:
        return ""
    seg = link.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    seg = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", seg)
    return re.sub(r"[^a-z0-9]+", " ", seg.lower()).strip()


def derivative_page_evidence(page_text: str, cfg: dict) -> list[str]:
    """
    Phrases on a competition's own page that show a derivatives track, e.g.
    Bitget CandyBomb's "Futures trading pool (new futures users only)".

    Only PARTICIPATION phrases count — pool / task / volume / position / first
    futures trade. Bare words like "futures" or "margin" are useless here: every
    Bitget page carries them in its menu and related-article list, and counting
    them would flag clean spot comps.
    """
    pats = (cfg.get("filter") or {}).get("derivative_page_phrases") or []
    found = []
    for p in pats:
        try:
            for m in re.finditer(p, page_text or "", re.I):
                s = m.group(0).lower()
                if s not in found:
                    found.append(s)
        except re.error:
            continue
    return found


def chance_only_evidence(page_text: str, cfg: dict) -> str | None:
    """
    The chance phrase that shows a promo pays by luck, not by trading rank —
    or None. A page that ranks traders anywhere is a real competition even if it
    also runs a side draw (Bitget Global Markets: leaderboard + mystery boxes).
    """
    f = cfg.get("filter") or {}

    def first(pats):
        for p in pats or []:
            try:
                m = re.search(p, page_text or "", re.I)
            except re.error:
                continue
            if m:
                return m.group(0).lower()
        return None

    hit = first(f.get("chance_page_phrases"))
    if hit and not first(f.get("ranking_page_phrases")):
        return hit
    return None


def classify_item(title: str, body: str, cfg: dict, link: str = "") -> dict:
    """
    Decide keep/drop and the type of a competition-like item.

    Rule set (config-driven):
      * hard excludes, derivative products in the title/link (margin, options,
        copy trading...) and futures/perps titles without a spot-trading phrase
        are always dropped.
      * An exclude keyword in the body removes the item, UNLESS the title carries
        a spot/swap/wallet signal — then it is KEPT and tagged 'mixed'.
      * include & not exclude  -> keep, 'onchain' if wallet/web3/swap else 'spot'
      * neither include nor exclude -> kept only if it scores >= score_threshold
        on the soft "looks like a comp" signals.
    Returns {keep, type, reason, score}.
    """
    f = cfg.get("filter") or {}
    text = _norm_text(title, body)

    def any_in(keys):
        hits = []
        for k in keys or []:
            kn = " ".join(k.lower().translate(_PUNCT).split())
            if kn and _word_search(kn, text):
                hits.append(k)
        return hits

    inc = any_in(f.get("include_keywords"))
    # regex includes catch phrasings with the token name in the middle
    # ("Trade AI-Sector DTFs to share $80,000") that literal keywords miss
    for pat in (f.get("include_patterns") or []):
        try:
            if re.search(pat, text, re.I):
                inc = inc + [f"pattern:{pat[:24]}"]
        except re.error:
            continue
    exc = any_in(f.get("exclude_keywords"))
    onchain = bool(any_in(f.get("onchain_keywords")))
    signal_hits = any_in(f.get("score_signals"))
    score = len(signal_hits)
    threshold = int(f.get("score_threshold", 2))

    # HARD excludes: content that is never a trading competition (giveaways, football
    # promos, meme contests, staking...). Nothing rescues these — not even a spot/wallet
    # word, because wallet-channel posts mention "wallet" in everything they publish.
    hard = any_in(f.get("hard_exclude_keywords"))
    if hard:
        return {"keep": False, "type": None, "score": score,
                "reason": f"hard-excluded by '{hard[0]}'"}

    def typ():
        return "onchain" if onchain else "spot"

    # Judge the instrument from the TITLE plus the LINK — what the event is branded
    # as — not the body. The link matters: KuCoin's "Trade to Win VIP Golf Bag" lived
    # at /activity/9thAnniversaryFuturesArena.
    title_text = _norm_text(title, _slug_words(link))

    # Derivative products named in the title/link are never spot competitions:
    # margin, options, copy trading, leverage, TradFi bots ("rToken Margin Trading
    # Carnival" came back as parts II and IV). Title-only on purpose — the body of a
    # real spot comp can say "profit margins" or "the option to join".
    deriv = [k for k in (f.get("derivative_title_keywords") or [])
             if _word_search(" ".join(k.lower().translate(_PUNCT).split()), title_text)]
    if deriv:
        return {"keep": False, "type": None, "score": score,
                "reason": f"derivative product in title/link: '{deriv[0]}'"}

    # Not-a-trading-competition words that are only safe to judge in the TITLE
    # (a real comp's Telegram post can mention "creators" or "feedback" in passing).
    tx = [k for k in (f.get("title_exclude_keywords") or [])
          if _word_search(" ".join(k.lower().translate(_PUNCT).split()), title_text)]
    if tx:
        return {"keep": False, "type": None, "score": score,
                "reason": f"not a trading competition (title: '{tx[0]}')"}
    for pat in (f.get("title_exclude_patterns") or []):
        try:
            if re.search(pat, title_text, re.I):
                return {"keep": False, "type": None, "score": score,
                        "reason": f"not a trading competition (title pattern: {pat[:28]})"}
        except re.error:
            continue

    # A real spot+futures combo must SAY spot trading in its title; a lone "spot"
    # doesn't count ("earn your spot" is a seat). Punctuation is stripped, so
    # "spot & futures" -> "spot futures".
    spot_combo = any(p in title_text for p in (
        "spot trading", "spot market", "spot pair", "spot futures", "futures spot",
        "spot and futures", "futures and spot", "spot swap"))
    is_futures_title = any(_word_search(w, title_text) for w in (
        "futures", "perp", "perpetual", "cfd", "usdt m", "coin m"))
    if is_futures_title and not spot_combo:
        return {"keep": False, "type": None, "score": score,
                "reason": "futures/perps in title or link (no spot-trading phrase)"}

    # What lets an item whose BODY mentions futures survive as "mixed". It must come
    # from the title: aggregator descriptions mention "spot" or "wallet" for almost
    # every futures league (Binance Traders League, Blofin Whale Era, Zoomex
    # Multi-Asset, KuCoin PROOF all leaked through the old body-wide check).
    spot_signal = spot_combo or bool(
        [k for k in ("swap", "wallet", "web3", "onchain", "dex", "candybomb", "candydrop")
         if _word_search(k, title_text)])

    if inc and exc:
        if spot_signal:
            return {"keep": True, "type": "mixed", "score": score,
                    "reason": f"spot signal + exclude({exc[0]}) -> mixed"}
        return {"keep": False, "type": None, "score": score,
                "reason": f"excluded by '{exc[0]}' (no spot signal; generic include only)"}
    if exc and not inc:
        return {"keep": False, "type": None, "score": score,
                "reason": f"excluded by '{exc[0]}'"}
    if inc and not exc:
        return {"keep": True, "type": typ(), "score": score,
                "reason": f"include '{inc[0]}'"}
    # neither include nor exclude
    if score >= threshold:
        return {"keep": True, "type": typ(), "score": score,
                "reason": f"score {score} ({', '.join(signal_hits[:3])})"}
    return {"keep": False, "type": None, "score": score,
            "reason": "no include keyword, score below threshold"}
