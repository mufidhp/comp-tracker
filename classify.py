"""
classify.py — pure rule-based filtering and legitimacy tiers.

Two jobs:
  1. Decide whether a scraped item is a spot/onchain trading competition we keep,
     and tag it spot / onchain / mixed.
  2. Map a venue name to a safety tier (A / caution / avoid).

No network, no AI. Used by Mode A and (for the hard-coded AVOID guard) Mode B.
"""
from __future__ import annotations

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


def classify_item(title: str, body: str, cfg: dict) -> dict:
    """
    Decide keep/drop and the type of a competition-like item.

    Rule set (config-driven):
      * An exclude keyword (futures/perps/staking/...) removes the item,
        UNLESS an include keyword (spot/swap/...) also matches — then it is
        KEPT and tagged 'mixed'.
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
            if kn and kn in text:
                hits.append(k)
        return hits

    inc = any_in(f.get("include_keywords"))
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

    # A "spot signal" is what lets an item survive a futures/exclude word as MIXED.
    # Generic words like "trading competition"/"tournament" do NOT count — otherwise a
    # futures-only "Perpetual Futures Trading Competition" would wrongly be kept.
    spot_signal = ("spot" in text) or onchain or bool(
        [k for k in ("swap to share", "swap & share", "swap and share", "candybomb",
                     "candydrop", "memebox", "meme go") if k in text])

    def typ():
        return "onchain" if onchain else "spot"

    # Judge futures/perps from the TITLE — what the event is branded as — not the body.
    # Rescue to "mixed" only for a REAL spot+futures combo: a lone "spot" doesn't count,
    # because idioms like "earn your spot" (a seat) falsely match. Require a spot-TRADING
    # phrase. (Punctuation is stripped, so "spot & futures" -> "spot futures".)
    title_text = _norm_text(title)
    is_futures_title = any(w in title_text for w in ("futures", "perp", "cfd"))
    spot_combo = any(p in title_text for p in (
        "spot trading", "spot market", "spot pair", "spot futures", "futures spot",
        "spot and futures", "futures and spot", "spot swap"))

    if inc and exc:
        if is_futures_title and not spot_combo:
            return {"keep": False, "type": None, "score": score,
                    "reason": "futures/perps only (no real spot-trading phrase in title)"}
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
