"""Karrd Kings deal alert.

Scans new eBay Buy It Now listings for the players in config.yml, prices each
one against recent sold auction comps from CardSight AI, and sends a phone push
notification (ntfy) when a card is listed well under market value.

Secrets (set as GitHub repository secrets):
  EBAY_APP_ID, EBAY_CERT_ID, CARDSIGHT_API_KEY, NTFY_TOPIC
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state.json"

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SPORTS_SINGLES_CATEGORY = "261328"
CARDSIGHT_SEARCH_URL = "https://api.cardsight.ai/v1/pricing/search"
NTFY_URL = "https://ntfy.sh/"

RUN_INTERVAL_MINUTES = 20  # must match the cron in .github/workflows/deal-alert.yml

GRADERS = ["PSA", "BGS", "SGC", "CGC", "CSG", "BCCG", "HGA", "TAG", "ISA", "GMA"]
GRADE_RE = re.compile(
    r"\b(" + "|".join(GRADERS) + r")\s*(?:GEM\s*(?:MINT|MT)?\s*|MINT\s*|MT\s*|PRISTINE\s*|BLACK LABEL\s*)?(10|[1-9](?:\.5)?)\b",
    re.I,
)
PARALLEL_HINTS = [
    "refractor", "silver", "gold", "orange", "red", "blue", "green",
    "purple", "pink", "black", "wave", "shimmer", "mojo", "cracked ice",
    "holo", "xfractor", "superfractor", "atomic", "sapphire", "velocity",
    "hyper", "disco", "camo", "tiger", "snakeskin", "lava", "scope",
]
NUMBERED_RE = re.compile(r"(?:#\s*)?/\s*\d{1,4}\b|\b\d{1,4}/\d{1,4}\b")


# ---------------------------------------------------------------- helpers

RUN_LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}"
    RUN_LOG.append(line)
    print(line, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen", {})
    state.setdefault("queue", [])
    state.setdefault("usage", {"month": "", "calls": 0, "credit": 0.0})
    state.setdefault("last_error_alert", "")
    return state


def save_state(state: dict) -> None:
    state["last_run_log"] = RUN_LOG[-80:]
    if ASK_CACHE:
        state["ask_cache"] = ASK_CACHE
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("```\n" + "\n".join(RUN_LOG) + "\n```\n")
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def title_has_excluded_word(title: str, words: list[str]) -> bool:
    t = " " + re.sub(r"[^a-z0-9/ ]+", " ", title.lower()) + " "
    return any(f" {w.lower()} " in t for w in words)


GRADE_BY_RE = re.compile(r"\b(10|[1-9](?:\.5)?)\s*(?:by|from)\s*(" + "|".join(GRADERS) + r")\b", re.I)
SLAB_WORDS = re.compile(r"\b(graded|slab|slabbed|" + "|".join(GRADERS) + r")\b", re.I)
RAW_HINTS = re.compile(r"\b(ungraded|not graded|raw|psa ready|psa candidate|gem candidate|grade it|send (?:it )?in)\b", re.I)


def detect_grade(title: str) -> tuple[str, str] | None:
    """Return (company, grade) if the title says the card is slabbed."""
    m = GRADE_RE.search(title)
    if m:
        return norm_grade(m.group(1), m.group(2))
    m = GRADE_BY_RE.search(title)  # e.g. "Graded 8 by SGC"
    if m:
        return norm_grade(m.group(2), m.group(1))
    return None


def is_graded(title: str, condition: str = "") -> bool | None:
    """True = slabbed, False = raw, None = can't tell. eBay's own condition field wins over the title."""
    c = (condition or "").lower()
    if c.startswith("graded"):
        return True
    if c.startswith("ungraded"):
        return False
    if detect_grade(title):
        return True
    if RAW_HINTS.search(title):
        return False
    if SLAB_WORDS.search(title):
        return None  # mentions grading but no grade we can read: don't guess
    return False


def norm_grade(company: str, value) -> tuple[str, str]:
    """Normalize e.g. ("Professional Sports Authenticator", "9.0") -> ("PSA", "9")."""
    c = str(company).upper()
    aliases = {"PROFESSIONAL SPORTS": "PSA", "BECKETT": "BGS", "SPORTSCARD GUARANTY": "SGC", "CERTIFIED GUARANTY": "CGC"}
    for k, v in aliases.items():
        if k in c:
            c = v
    for grader in GRADERS:
        if re.search(rf"\b{grader}\b", c):
            c = grader
            break
    try:
        v = f"{float(value):g}"
    except (TypeError, ValueError):
        v = str(value).strip()
    return c, v


def looks_like_parallel(title: str) -> bool:
    t = title.lower()
    return bool(NUMBERED_RE.search(t)) or any(re.search(rf"\b{w}\b", t) for w in PARALLEL_HINTS)


def parallel_words_in_title(parallel_name: str, title: str) -> bool:
    t = title.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", parallel_name.lower()) if len(w) > 2]
    return all(w in t for w in words) if words else True


# ---------------------------------------------------------------- budget

def runs_left_in_month(now: datetime) -> int:
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    end = now.replace(day=days_in_month, hour=23, minute=59, second=59)
    return max(1, int((end - now).total_seconds() // (RUN_INTERVAL_MINUTES * 60)) + 1)


def checks_allowed_this_run(state: dict, cfg: dict, now: datetime) -> int:
    """Spread the monthly CardSight budget evenly across the remaining runs."""
    usage = state["usage"]
    month = now.strftime("%Y-%m")
    if usage["month"] != month:
        usage.update(month=month, calls=0, credit=0.0)
    remaining = max(0, int(cfg["monthly_budget"]) - usage["calls"])
    if remaining == 0:
        return 0
    burst_until = cfg.get("burst_until")
    if burst_until and str(burst_until).strip() and now < datetime.fromisoformat(str(burst_until)):
        return min(int(cfg.get("burst_checks_per_run", 5)), remaining)  # trial: spend faster, still capped
    usage["credit"] = min(usage["credit"] + remaining / runs_left_in_month(now), 25.0)
    return min(int(usage["credit"]), remaining)


def spend(state: dict, n: int = 1) -> None:
    state["usage"]["calls"] += n
    state["usage"]["credit"] = max(0.0, state["usage"]["credit"] - n)


# ---------------------------------------------------------------- eBay

def ebay_token(app_id: str, cert_id: str) -> str:
    auth = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    r = requests.post(
        EBAY_TOKEN_URL,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def ebay_new_listings(token: str, player: str, cfg: dict) -> list[dict]:
    price_filter = f"price:[{cfg['min_price']}..{cfg['max_price']}],priceCurrency:USD"
    params = {
        "q": player,
        "category_ids": EBAY_SPORTS_SINGLES_CATEGORY,
        "sort": "newlyListed",
        "limit": 50,
        "filter": f"buyingOptions:{{FIXED_PRICE}},{price_filter},itemLocationCountry:US",
    }
    r = requests.get(
        EBAY_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    items = []
    for it in r.json().get("itemSummaries", []):
        try:
            price = float(it["price"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        ship = 0.0
        for opt in it.get("shippingOptions") or []:
            try:
                ship = float(opt["shippingCost"]["value"])
                break
            except (KeyError, TypeError, ValueError):
                pass
        items.append({
            "id": it["itemId"],
            "title": it.get("title", ""),
            "price": price,
            "shipping": ship,
            "url": it.get("itemWebUrl", ""),
            "image": (it.get("image") or {}).get("imageUrl", ""),
            "condition": it.get("condition", ""),
            "player": player,
            "found_at": datetime.now(timezone.utc).isoformat(),
        })
    return items


def ebay_ending_auctions(token: str, player: str, cfg: dict) -> list[dict]:
    """Auctions for this player ending within the next auction_window_minutes, with few bids."""
    now = datetime.now(timezone.utc)
    end = (now + timedelta(minutes=int(cfg.get("auction_window_minutes", 45)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "q": player,
        "category_ids": EBAY_SPORTS_SINGLES_CATEGORY,
        "sort": "endingSoonest",
        "limit": 100,
        "filter": f"buyingOptions:{{AUCTION}},itemEndDate:[..{end}],price:[0..{cfg['max_price']}],"
                  f"priceCurrency:USD,itemLocationCountry:US",
    }
    r = requests.get(
        EBAY_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    items = []
    for it in r.json().get("itemSummaries", []):
        try:
            bid = float((it.get("currentBidPrice") or it.get("price"))["value"])
            end_at = datetime.fromisoformat(it["itemEndDate"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        bids = int(it.get("bidCount") or 0)
        if bids > int(cfg.get("auction_max_bids", 5)) or end_at < now + timedelta(minutes=3):
            continue
        ship = 0.0
        for opt in it.get("shippingOptions") or []:
            try:
                ship = float(opt["shippingCost"]["value"])
                break
            except (KeyError, TypeError, ValueError):
                pass
        items.append({
            "id": it["itemId"], "kind": "auction", "title": it.get("title", ""), "price": bid,
            "condition": it.get("condition", ""),
            "shipping": ship, "bids": bids, "ends_at": end_at.isoformat(), "url": it.get("itemWebUrl", ""),
            "player": player, "found_at": now.isoformat(),
        })
    return items


def ebay_ask_ratio(token: str, listing: dict, query: str, cfg: dict) -> float | None:
    """Free pre-check: listing price vs the median of other Buy It Now listings of the same card.

    Returns price / median-ask (0.5 = half of what others are asking), or None when
    there aren't enough comparable listings to judge.
    """
    r = requests.get(
        EBAY_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": query, "category_ids": EBAY_SPORTS_SINGLES_CATEGORY, "limit": 50,
                "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD"},
        timeout=30,
    )
    r.raise_for_status()
    grade, num, par = detect_grade(listing["title"]), card_number(listing["title"]), looks_like_parallel(listing["title"])
    asks = []
    for it in r.json().get("itemSummaries", []):
        t = it.get("title", "")
        if it.get("itemId") == listing["id"] or title_has_excluded_word(t, cfg["exclude_words"]):
            continue
        if is_graded(t, it.get("condition", "")) != is_graded(listing["title"], listing.get("condition", "")):
            continue  # raw only vs raw, slabs only vs slabs
        if detect_grade(t) != grade or (num and card_number(t) != num) or looks_like_parallel(t) != par:
            continue
        if subsets_in(t) != subsets_in(listing["title"]):
            continue
        if parallel_hints(t) != parallel_hints(listing["title"]) or sub_products(t) != sub_products(listing["title"]):
            continue  # a Blue parallel isn't compared with Gold ones; Chrome isn't compared with base
        try:
            price = float(it["price"]["value"])
            ship = float(((it.get("shippingOptions") or [{}])[0].get("shippingCost") or {}).get("value", 0))
        except (KeyError, TypeError, ValueError):
            continue
        asks.append(price + ship)
    if len(asks) < 3:
        return None
    asks.sort()
    low_quartile = asks[len(asks) // 4]  # asks run high; compare against the cheaper end
    listing["ask_ref"] = low_quartile
    listing["ask_n"] = len(asks)
    return (listing["price"] + listing["shipping"]) / low_quartile


ASK_CACHE: dict = {}  # filled from state at start of run


def ask_ratio_cached(token: str, listing: dict, cfg: dict, now: datetime) -> float | None:
    """Same as ebay_ask_ratio, but reuses what other sellers ask for this card for a few hours."""
    key = listing["query"]
    hit = ASK_CACHE.get(key)
    if hit and now - datetime.fromisoformat(hit["at"]) < timedelta(hours=float(cfg.get("ask_cache_hours", 6))):
        if hit["ref"] is None:
            return None
        listing["ask_ref"] = hit["ref"]
        listing["ask_n"] = hit.get("n", 0)
        return (listing["price"] + listing["shipping"]) / hit["ref"]
    ratio = ebay_ask_ratio(token, listing, key, cfg)
    ASK_CACHE[key] = {"ref": listing.get("ask_ref") if ratio is not None else None, "n": listing.get("ask_n", 0),
                      "at": now.isoformat()}
    return ratio


# ---------------------------------------------------------------- CardSight comps

BRANDS = [
    "topps", "bowman", "bbm", "epoch", "panini", "fleer", "upper deck", "donruss", "score", "leaf", "skybox", "hoops",
    "prizm", "select", "optic", "mosaic", "chrome", "finest", "stadium club", "heritage", "now",
    "national treasures", "flawless", "immaculate", "contenders", "spectra", "obsidian", "phoenix",
    "revolution", "court kings", "chronicles", "certified", "absolute", "crown royale", "origins",
    "noir", "one and one", "black", "sapphire", "update", "allen ginter", "gypsy queen", "tribute",
    "dynasty", "museum", "sterling", "inception", "archives", "big league", "opening day",
    "collector's choice", "sp authentic", "spx", "sp", "exquisite", "ultra", "flair", "metal",
    "e-x", "bowman's best", "draft", "cosmic", "stadium", "zenith", "illusions", "rookies & stars",
    "co signers", "co signer", "triple threads", "five star", "luminaries", "tier one", "dynasty",
    "gallery", "fire", "stars", "pristine", "high tek", "clearly authentic", "signature", "playoff",
]
CARD_NO_RE = re.compile(r"#\s*([A-Za-z]{0,6}-?\d{1,4}[A-Za-z]?)\b|\bno\.?\s*(\d{1,4})\b", re.I)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("'s", "s"))


CODE_NO_RE = re.compile(r"\b([A-Z]{1,6}-[A-Z]{0,3}\d{1,4}[A-Z]?)\b")


def surname(player: str) -> str:
    parts = [w for w in player.split() if w.lower().strip(".") not in ("jr", "sr", "ii", "iii")]
    return parts[-1]


def search_name(player: str) -> str:
    """Name without Jr./Sr. so titles like 'Ken Griffey 1989 Upper Deck' still match."""
    return " ".join(w for w in player.split() if w.lower().strip(".") not in ("jr", "sr", "ii", "iii"))


def grade_clear(item: dict) -> bool:
    """Skip listings where we can't tell raw vs graded, or it's graded but the grade isn't readable."""
    g = is_graded(item["title"], item.get("condition", ""))
    return g is False or (g is True and detect_grade(item["title"]) is not None)


def player_rule_ok(title: str, player: str, cfg: dict) -> bool:
    """Per-player filters, e.g. keep Ken Griffey Sr. cards out of a Ken Griffey Jr. search."""
    rule = (cfg.get("player_rules") or {}).get(player) or {}
    if rule.get("exclude") and title_has_excluded_word(title, rule["exclude"]):
        return False
    m = re.search(r"\b(19[3-9]\d|20[0-3]\d)\b", title)
    if rule.get("min_year") and m and int(m.group(1)) < int(rule["min_year"]):
        return False
    return True


def card_number(title: str) -> str | None:
    m = CARD_NO_RE.search(title)
    if m:
        return (m.group(1) or m.group(2)).upper().lstrip("0") or "0"
    m = CODE_NO_RE.search(title)  # insert codes written without '#', e.g. SMLB-9, BTP-2
    if m and not re.fullmatch(r"(19|20)\d\d-\d\d", m.group(1)):
        return m.group(1).upper()
    return None


def build_query(title: str, player: str) -> str:
    """Short, clean search: year + brands + player + card # + parallel words + grade.

    CardSight's title search needs every word to match, so eBay filler
    (team names, RC, HOF, emojis...) must be left out.
    """
    t = " ".join(words(title))
    parts = []
    m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", title)
    if m:
        parts.append(m.group(1))
    for b in BRANDS:
        bw = " ".join(words(b))
        if re.search(rf"\b{re.escape(bw)}\b", t) and bw not in parts:
            parts.append(" ".join(w for w in b.split() if "'" not in w))
    parts.append(search_name(player))
    num = card_number(title)
    if num:
        parts.append(num)
    for w in PARALLEL_HINTS:
        if re.search(rf"\b{w}\b", t):
            parts.append(w)
    g = detect_grade(title)
    if g:
        parts += [g[0], g[1]]
    return " ".join(parts)[:300]


def cardsight_comps(api_key: str, query: str, cfg: dict) -> dict:
    r = requests.get(
        CARDSIGHT_SEARCH_URL,
        headers={"X-API-Key": api_key},
        params={"q": query, "listing_type": "auction", "period": cfg["comp_period"], "limit": 100},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def identity_ok(rec: dict, listing_title: str, title_grade, title_num) -> bool:
    card = rec.get("matched_card")
    if not card:
        return False
    g = rec.get("grade")
    if (norm_grade(g["company_name"], g["grade_value"]) if g else None) != title_grade:
        return False  # raw vs slab (or different grade) must line up with the listing
    if rec.get("title") and (detect_grade(rec["title"]) != title_grade
                             or (title_grade is None and is_graded(rec["title"]) is not False)):
        return False  # CardSight sometimes misses the grade; trust the sale title too
    lyr = (re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", listing_title) or [None])[0]
    cyr = str((card.get("set") or {}).get("year") or "")[:4]
    if lyr and cyr and lyr != cyr:
        return False  # e.g. a 1992 card offered as comps for a 1988 listing
    if title_num and str(card.get("number") or "").upper().lstrip("0") != title_num:
        return False
    tw = set(words(listing_title))
    squashed = "".join(words(listing_title))  # so "Project70" matches "Project 70"

    def present(w: str) -> bool:
        return w in tw or w in squashed

    cset = card.get("set") or {}
    release_words = [w for w in words(cset.get("release") or "") if len(w) > 1]
    if release_words and not all(present(w) for w in release_words):
        return False  # e.g. comp is "Topps Chrome Black" but listing is plain Topps
    set_name = (cset.get("name") or "").lower()
    if set_name and set_name not in ("base", "base set"):
        if not all(present(w) for w in words(set_name) if len(w) > 2 and w not in ("set", "cards")):
            return False  # insert set named in comp but not in listing
    pname = rec.get("parallel_name")
    if pname and not parallel_words_in_title(pname, listing_title):
        return False
    if not pname and looks_like_parallel(listing_title):
        return False  # listing looks like a parallel but comp is base
    return True


MAKERS = {"topps", "panini", "fleer", "upper deck", "skybox", "bowman", "donruss", "leaf", "score", "hoops",
          "bbm", "epoch", "sp", "black", "stars", "signature", "fire"}


def sub_products(title: str) -> set[str]:
    """Product lines beyond the maker, e.g. {'chrome'} for Topps Chrome, {'co signer'} for Topps Co-Signers."""
    found = brands_in(title.replace("-", " ")) - MAKERS
    return {f[:-1] if f.endswith("s") and len(f) > 4 else f for f in found}


def brands_in(title: str) -> set[str]:
    t = " " + " ".join(words(title)) + " "
    return {b for b in BRANDS if f" {' '.join(words(b))} " in t}


SUBSET_PHRASES = [
    "fresh faces", "all rookie", "all rookies", "rookie sensations", "star power", "die cut", "diecut",
    "gold medallion", "precious metal", "rookie rage", "decade of excellence", "all stars", "all star",
    "power surge", "hot pack", "clutch gene", "downtown", "kaboom", "color blast", "stained glass",
    "instant impact", "rated rookie", "future watch", "team leaders", "league leaders", "highlights",
    "checklist", "retro", "throwback", "anniversary", "variation", "sp variation", "ssp", "image variation",
    "photo variation", "fanatics", "game used", "jersey", "patch", "relic", "auto", "autograph",
    "members only", "member only", "reprint", "finest", "golden season", "season's best", "power in the key",
    "electric court", "gold medallion", "precious metal gems", "credential", "press proof",
]

# Words that usually mean a different (often pricier) version of a card. Used to clean the sold-comps link.
_TOP_VARIANTS = ["finest", "members only", "reprint", "refractor", "chrome", "auto", "patch", "parallel", "variation",
                 "insert", "gold", "silver", "prizm", "lot", "custom", "rp", "sp", "ssp", "proof", "die cut"]
VARIANT_TERMS = _TOP_VARIANTS + sorted(({p for p in SUBSET_PHRASES} | set(PARALLEL_HINTS)) - set(_TOP_VARIANTS))


def _sing(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def subsets_in(title: str) -> set[str]:
    # singularize so "Variations" matches "variation", "All-Rookies" matches "all rookie"
    t = " " + " ".join(_sing(w) for w in words(title.replace("-", " "))) + " "
    found = {" ".join(_sing(w) for w in p.split()) for p in SUBSET_PHRASES}
    found = {p for p in found if f" {p} " in t}
    # a second year in the title marks a retro/throwback design, e.g. "2008-09 Topps ... 1958-59 Variations"
    years = re.findall(r"\b(19[3-9]\d|20[0-3]\d)\b", title)
    if years:
        found |= {f"design {y}" for y in years[1:] if abs(int(y) - int(years[0])) > 1}
    return found


def parallel_hints(title: str) -> set[str]:
    t = title.lower()
    return {w for w in PARALLEL_HINTS if re.search(rf"\b{w}\b", t)}


def title_match_ok(rec_title: str, listing_title: str, title_grade, title_num) -> bool:
    """For sold comps CardSight couldn't tie to a catalog card: compare titles directly."""
    if not title_num or card_number(rec_title) != title_num:
        return False
    if detect_grade(rec_title) != title_grade:
        return False
    if title_grade is None and is_graded(rec_title) is not False:
        return False
    if looks_like_parallel(rec_title) != looks_like_parallel(listing_title):
        return False
    hints = lambda t: {w for w in PARALLEL_HINTS if re.search(rf"\b{w}\b", t.lower())}
    if hints(rec_title) != hints(listing_title):
        return False
    yr = lambda t: (re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", t) or [None])[0]
    if yr(rec_title) != yr(listing_title):
        return False
    return brands_in(rec_title) - {"panini"} == brands_in(listing_title) - {"panini"}


FILLER = set("""
a an the of and in on with w by for to from x vs
rc rookie rookies card cards hof goat mvp legend legends icon nba mlb nfl basketball baseball football sport sports
psa bgs sgc cgc csg tag hga isa gma beckett graded grade gem mint mt nm ex vg pristine black label auth authentic
raw ungraded slab slabbed sharp centered centering corners perfect clean nice beautiful wow look rare invest
investment vintage hot new lot insert inserts base set sp ssp short print parallel numbered serial pop low
team teams chicago los angeles la bulls lakers cavaliers cavs cleveland heat miami warriors golden state san antonio
spurs dodgers angels mariners seattle reds royals kansas city patriots buccaneers tampa bay ny new york yankees
houston rockets wizards washington bulls hornets charlotte philadelphia bos boston
premier level class image photo variation variations retro design
""".split())


def distinctive(title: str, player: str = "") -> set[str]:
    """Words that identify WHICH card it is (insert/subset names), after dropping filler, names, brands, numbers."""
    drop = set(words(player)) | {w for b in BRANDS for w in words(b)} | FILLER
    drop |= {_sing(w) for w in drop}
    out = set()
    for raw in words(title.replace("-", " ").replace("/", " ")):
        w = _sing(raw)
        if any(ch.isdigit() for ch in w) or len(w) < 3 or raw in drop or w in drop:
            continue
        out.add(w)
    return out


def trim(prices) -> list[float]:
    """Drop sales far from the middle (mislabeled cards, shill bids, damaged copies)."""
    p = sorted(prices)
    if len(p) < 3:
        return p
    m = statistics.median(p)
    return [x for x in p if 0.5 * m <= x <= 2 * m]


def market_value(listing_title: str, results: list[dict], cfg: dict, player: str = "") -> dict | None:
    """Find sold comps that are the same card, parallel and grade as the listing.

    Groups matching comps by exact identity and uses the biggest group. Returns None
    when nothing lines up or there are too few comps to trust.
    """
    title_grade = detect_grade(listing_title)
    title_num = card_number(listing_title)
    if not title_num:
        return None  # without a card # we can't be sure which card it is (e.g. an insert vs the base card)
    groups: dict[tuple, list[dict]] = {}
    unmatched: list[dict] = []
    listing_subsets = subsets_in(listing_title)
    listing_products = sub_products(listing_title)
    player_hint = player or ""
    listing_words = distinctive(listing_title, player_hint)
    for rec in results:
        if rec.get("listing_type", "auction") != "auction" or not rec.get("price"):
            continue
        if rec.get("title") and subsets_in(rec["title"]) != listing_subsets:
            continue  # e.g. "Fresh Faces #3" ($450) is a different card from "All-Rookies #3" ($50)
        if rec.get("title") and sub_products(rec["title"]) != listing_products:
            continue  # e.g. base 2008-09 Topps #23 ($275) vs 2008-09 Topps Co-Signers #23 ($3)
        if rec.get("title"):
            cw = distinctive(rec["title"], player_hint)
            if len(cw - listing_words) >= 2:
                continue  # sale names an insert the listing doesn't: "Power in the Key #2" ($500) vs "All-NBA Team #2"
        if not rec.get("matched_card"):
            if title_match_ok(rec.get("title") or "", listing_title, title_grade, title_num):
                unmatched.append(rec)
            continue
        if not identity_ok(rec, listing_title, title_grade, title_num):
            continue
        key = (rec["matched_card"]["card_id"], rec.get("parallel_id"), (rec.get("grade") or {}).get("grade_id"))
        groups.setdefault(key, []).append(rec)
    if unmatched:
        if groups:
            biggest = max(groups, key=lambda k: len(groups[k]))
            m = statistics.median(float(r["price"]) for r in groups[biggest])
            groups[biggest] = groups[biggest] + [r for r in unmatched if 0.5 * m <= float(r["price"]) <= 2 * m]
        else:
            groups[("title-match", None, None)] = unmatched
    if not groups:
        sample = [
            f"{(r.get('matched_card') or {}).get('set', {}).get('release', '?')} "
            f"#{(r.get('matched_card') or {}).get('number', '?')}|{r.get('parallel_name') or 'base'}|"
            f"{(r.get('grade') or {}).get('company_name', 'raw')} {(r.get('grade') or {}).get('grade_value', '')}"
            for r in results[:4]
        ]
        log(f"  no matching comps (grade {title_grade}, #{title_num}); {len(results)} results, top: {sample}")
        return None
    recs = max(groups.values(), key=len)
    if listing_words:
        # prefer sales that name the same insert/subset as the listing, when there are enough of them
        strong = [r for r in recs if r.get("title") and listing_words <= distinctive(r["title"], player_hint)]
        if len(strong) >= int(cfg["min_comps"]):
            recs = strong
    if len(recs) < int(cfg["min_comps"]):
        log(f"  only {len(recs)} comps for matched card")
        return None
    top = next((r for r in recs if r.get("matched_card")), None)
    if top is None:
        prices = trim(float(r["price"]) for r in recs)
        if len(prices) < int(cfg["min_comps"]):
            return None
        label = f"title match: {recs[0].get('title', '')[:60]}"
        return {"median": statistics.median(prices), "count": len(prices), "label": label}
    card = top["matched_card"]
    cset = card.get("set") or {}
    label = " ".join(x for x in [
        str(cset.get("year") or ""), cset.get("release") or "",
        cset.get("name") if (cset.get("name") or "").lower() not in ("base", "base set") else "",
        card.get("name", ""), f"#{card['number']}" if card.get("number") else "", top.get("parallel_name") or "",
    ] if x)
    if title_grade:
        label += f" {title_grade[0]} {title_grade[1]}"
    prices = trim(float(r["price"]) for r in recs)
    if len(prices) < int(cfg["min_comps"]):
        log(f"  only {len(prices)} comps after dropping outliers")
        return None
    sample = [f"${float(r['price']):,.0f} {(r.get('title') or '')[:55]}" for r in recs[:2] if r.get("title")]
    return {"median": statistics.median(prices), "count": len(prices), "label": label.strip(), "sample": sample}


def evaluate(listing: dict, mv: dict, cfg: dict) -> dict | None:
    cost = listing["price"] + listing["shipping"]
    median = mv["median"]
    discount = 1 - cost / median
    if discount < float(cfg["discount_threshold"]):
        return None
    profit = median * (1 - float(cfg["fee_rate"])) - cost
    return {"cost": cost, "discount": discount, "profit": profit}


# ---------------------------------------------------------------- notifications

def notify(topic: str, title: str, body: str, url: str = "", priority: str = "high", tags: str = "moneybag",
           sold_url: str = "") -> None:
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Open listing, {url}" + (f"; view, Sold comps, {sold_url}" if sold_url else "")
    r = requests.post(NTFY_URL + topic, data=body.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()


def deal_message(listing: dict, mv: dict, ev: dict, cfg: dict | None = None) -> tuple[str, str]:
    if listing.get("kind") == "auction":
        mins = max(0, int((datetime.fromisoformat(listing["ends_at"]) - datetime.now(timezone.utc)).total_seconds() // 60))
        threshold = float((cfg or {}).get("discount_threshold", 0.35))
        max_bid = mv["median"] * (1 - threshold) - listing["shipping"]
        title = f"AUCTION ends in {mins} min - {listing['player']}"
        body = (
            f"{listing['title']}\n\n"
            f"Current bid: ${listing['price']:,.2f} ({listing['bids']} bids) + ${listing['shipping']:,.2f} ship\n"
            f"Sold comps: ${mv['median']:,.2f} median ({mv['count']} sales)\n"
            f"Bid up to ${max_bid:,.2f} to stay {threshold:.0%} under market\n"
            f"Matched: {mv['label']}"
        )
        if mv.get("sample"):
            body += "\nSold examples:\n" + "\n".join(mv["sample"])
        return title, body
    title = f"{ev['discount']:.0%} under comps - {listing['player']}"
    body = (
        f"{listing['title']}\n\n"
        f"Price: ${listing['price']:,.2f} + ${listing['shipping']:,.2f} ship\n"
        f"Sold comps: ${mv['median']:,.2f} median ({mv['count']} sales)\n"
        f"Est. profit after fees: ${ev['profit']:,.2f}\n"
        f"Matched: {mv['label']}"
    )
    if mv.get("sample"):
        body += "\nSold examples:\n" + "\n".join(mv["sample"])
    if ev["discount"] >= 0.70:
        body += "\n\nWARNING: 70%+ under market. Check photos closely for reprint, damage or wrong card."
    return title, body


def sold_search_url(listing: dict) -> str:
    """eBay sold search for this exact card, with other versions (Finest, Members Only, Refractor...) excluded."""
    from urllib.parse import quote_plus
    full = listing.get("query") or listing["title"][:80]  # plain search; you judge the versions yourself
    return ("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(full) + f"&_sacat={EBAY_SPORTS_SINGLES_CATEGORY}"
            "&LH_Sold=1&LH_Complete=1")


def ask_alert_message(listing: dict, cfg: dict, mv: dict | None = None) -> tuple[str, str]:
    ref, cost = listing["ask_ref"], listing["price"] + listing["shipping"]
    off = 1 - cost / ref
    n = listing.get("ask_n") or 0
    if listing.get("kind") == "auction":
        mins = max(0, int((datetime.fromisoformat(listing["ends_at"]) - datetime.now(timezone.utc)).total_seconds() // 60))
        max_bid = ref * (1 - float(cfg.get("ask_discount", 0.35))) - listing["shipping"]
        if mv:  # real sold prices beat asking prices: stay 30% under what it actually sells for
            max_bid = min(max_bid, mv["median"] * 0.7 - listing["shipping"])
        title = f"AUCTION ends in {mins} min - {listing['player']}"
        body = (f"{listing['title']}\n\n"
                f"Current bid: ${listing['price']:,.2f} ({listing.get('bids', 0)} bids) + ${listing['shipping']:,.2f} ship\n"
                f"Other sellers ask: ${ref:,.2f}+ (cheaper end of {n} listings)\n"
                f"Bid up to ${max_bid:,.2f} to leave room for profit\n"
                f"Tap 'Sold comps' to check real sales before bidding.")
    else:
        title = f"{off:.0%} under other listings - {listing['player']}"
        body = (f"{listing['title']}\n\n"
                f"Price: ${listing['price']:,.2f} + ${listing['shipping']:,.2f} ship\n"
                f"Other sellers ask: ${ref:,.2f}+ (cheaper end of {n} listings)\n"
                f"Tap 'Sold comps' to check real sales before buying.")
    if off >= 0.75 and listing.get("kind") != "auction":
        body += "\n\nWARNING: far below everyone else. Check photos for reprint, damage or wrong card."
    return title, body


def ask_mode_alerts(state: dict, cfg: dict, env: dict, now: datetime, auctions: list[dict]) -> int:
    """Alert on listings priced well below what other sellers ask for the same card. No CardSight calls."""
    cut = 1 - float(cfg.get("ask_discount", 0.35))
    alerted = state.setdefault("alerted", {})
    cands = []
    for q in auctions + state["queue"]:
        r = q.get("ask_ratio")
        if r is None or r > cut or f"x:{q['id']}" in alerted:
            continue
        if q.get("kind") == "auction" and q["ask_ref"] < float(cfg["min_price"]):
            continue  # cheap card: not worth the time
        cands.append(q)
    # biggest dollar gap first, capped so the phone doesn't get spammed
    # Buy It Now first (that price is real; an auction bid will rise), then biggest dollar gap
    cands.sort(key=lambda q: (q.get("kind") == "auction", -(q["ask_ref"] - q["price"] - q["shipping"])))
    sent = 0
    cache = state.setdefault("comp_cache", {})
    # Sold-price checks: spend the month's remaining CardSight calls at an even daily pace.
    u = state["usage"]
    month = now.strftime("%Y-%m")
    if u.get("month") != month:
        u.update(month=month, calls=0, credit=0.0)
    today = now.strftime("%Y-%m-%d")
    if u.get("day") != today:
        u["day"], u["day_calls"] = today, 0
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    daily_cap = max(1, (int(cfg["monthly_budget"]) - u["calls"]) // days_left)
    boost_until = str(cfg.get("boost_until") or "").strip()
    if boost_until and now < datetime.fromisoformat(boost_until):
        daily_cap = int(cfg.get("boost_daily_checks", daily_cap))  # e.g. a weekend push for more alerts
    checks = max(0, min(daily_cap - u["day_calls"], int(cfg["monthly_budget"]) - u["calls"]))
    if not cfg.get("verify_with_cardsight", True):
        checks = 0
    require = bool(cfg.get("require_sold_confirmation", True))
    for q in cands:
        if sent >= int(cfg.get("max_alerts_per_run", 3)):
            break
        q.setdefault("query", build_query(q["title"], q["player"]))
        cost = q["price"] + q["shipping"]
        mv = None
        if cfg.get("verify_with_cardsight", True):
            if q["query"] in cache:
                mv = cache[q["query"]]["mv"]
            elif checks > 0:
                try:
                    data = cardsight_comps(env["CARDSIGHT_API_KEY"], q["query"], cfg)
                    mv = market_value(q["title"], data.get("results", []), cfg, q.get("player", ""))
                    cache[q["query"]] = {"mv": mv, "at": now.isoformat()}
                except Exception as e:  # noqa: BLE001
                    log(f"CardSight check failed: {e}")
                spend(state)
                u["day_calls"] += 1
                checks -= 1
        if require and not mv:
            # no reliable sold prices (CardSight found none, errored, or no calls left): never alert on asks alone
            if q["query"] in cache:
                alerted[f"x:{q['id']}"] = now.isoformat()
            log(f"  no sold-price confirmation: {q['title'][:60]}")
            continue
        # If real sold prices show this is just market price, skip it.
        if mv and cost > float(cfg.get("veto_if_over_sold", 0.8)) * mv["median"] and q.get("kind") != "auction":
            alerted[f"x:{q['id']}"] = now.isoformat()
            log(f"  vetoed by sold comps: ${cost:.2f} vs sold median ${mv['median']:.2f} ({mv['count']}): {q['title'][:60]}")
            continue
        t, body = ask_alert_message(q, cfg, mv)
        if mv and q.get("kind") != "auction":
            t = f"{1 - cost / mv['median']:.0%} under sold - {q['player']}"
        if mv:
            body += f"\nSold median: ${mv['median']:,.2f} ({mv['count']} sales)"
            if mv.get("sample"):
                body += "\nSold examples:\n" + "\n".join(mv["sample"])
        try:
            notify(env["NTFY_TOPIC"], t, body, url=q["url"], sold_url=sold_search_url(q))
            alerted[f"x:{q['id']}"] = now.isoformat()
            sent += 1
            log(f"ALERT {t}: {q['title'][:70]} (${q['price'] + q['shipping']:.2f} vs asks ${q['ask_ref']:.2f})")
        except Exception as e:  # noqa: BLE001
            log(f"notification failed: {e}")
    for a in auctions:
        if "ask_ratio" in a:
            state["seen"][f"a:{a['id']}"] = now.isoformat()
    # BIN listings that have been compared are done; keep only ones still waiting for a comparison
    state["queue"] = [q for q in state["queue"] if "ask_ratio" not in q]
    cutoff = now - timedelta(days=3)
    state["alerted"] = {k: v for k, v in alerted.items() if datetime.fromisoformat(v) > cutoff}
    return sent


def error_alert(state: dict, topic: str, msg: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_error_alert") == today or not topic:
        return
    try:
        notify(topic, "Deal alert problem", msg, priority="default", tags="warning")
        state["last_error_alert"] = today
    except Exception as e:  # noqa: BLE001
        log(f"could not send error alert: {e}")


# ---------------------------------------------------------------- main

def run() -> int:
    cfg = load_config()
    state = load_state()
    env = {k: os.environ.get(k, "").strip() for k in ["EBAY_APP_ID", "EBAY_CERT_ID", "CARDSIGHT_API_KEY", "NTFY_TOPIC"]}
    missing = [k for k, v in env.items() if not v]
    if missing:
        log(f"Missing secrets: {', '.join(missing)}")
        return 1

    if os.environ.get("TEST_NOTIFICATION") == "true":
        notify(env["NTFY_TOPIC"], "Deal alert is connected", "Test from Karrd Kings deal alert. Real deals will look like this.")
        log("sent test notification")

    now = datetime.now(timezone.utc)

    # prune seen ids older than 3 days and stale queue items
    cutoff = now - timedelta(days=3)
    state["seen"] = {k: v for k, v in state["seen"].items() if datetime.fromisoformat(v) > cutoff}
    max_age = timedelta(hours=float(cfg["max_listing_age_hours"]))
    players = set(cfg["players"])
    lo, hi = float(cfg["min_price"]), float(cfg["max_price"])
    state["queue"] = [
        q for q in state["queue"]
        if now - datetime.fromisoformat(q["found_at"]) < max_age and q["player"] in players and lo <= q["price"] <= hi
    ]  # also drops anything that no longer fits config.yml

    # 1. pull new listings from eBay (free)
    try:
        token = ebay_token(env["EBAY_APP_ID"], env["EBAY_CERT_ID"])
    except Exception as e:  # noqa: BLE001
        log(f"eBay auth failed: {e}")
        error_alert(state, env["NTFY_TOPIC"], f"eBay login failed: {e}")
        save_state(state)
        return 1

    new = 0
    for player in cfg["players"]:
        try:
            listings = ebay_new_listings(token, player, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"eBay search failed for {player}: {e}")
            continue
        log(f"eBay: {len(listings)} listings for {player}")
        for it in listings:
            if it["id"] in state["seen"]:
                continue
            state["seen"][it["id"]] = now.isoformat()
            if surname(player).lower() not in it["title"].lower():
                continue
            if title_has_excluded_word(it["title"], cfg["exclude_words"]) or not player_rule_ok(it["title"], player, cfg):
                continue
            if not grade_clear(it):
                continue
            if not card_number(it["title"]):
                continue  # no card # in the title: too easy to price the wrong card
            state["queue"].append(it)
            new += 1
    log(f"{new} new listings queued, {len(state['queue'])} in queue")

    auctions = []
    if cfg.get("auctions", True):
        for player in cfg["players"]:
            try:
                found = ebay_ending_auctions(token, player, cfg)
            except Exception as e:  # noqa: BLE001
                log(f"eBay auction search failed for {player}: {e}")
                continue
            for it in found:
                if f"a:{it['id']}" in state["seen"] or surname(player).lower() not in it["title"].lower():
                    continue
                if title_has_excluded_word(it["title"], cfg["exclude_words"]) or not card_number(it["title"]):
                    continue
                if not player_rule_ok(it["title"], player, cfg) or not grade_clear(it):
                    continue
                it["query"] = build_query(it["title"], player)
                auctions.append(it)
        if cfg.get("precheck_priority") == "price":
            auctions.sort(key=lambda a: -a["price"])
        else:
            auctions.sort(key=lambda a: a["ends_at"])
        log(f"{len(auctions)} auctions ending soon with <= {cfg.get('auction_max_bids', 5)} bids")

    # 2. free pre-check: compare each new listing to other eBay asks for the same card
    ask_mode = cfg.get("mode", "comps") == "asks"
    ask_cutoff = float(cfg.get("precheck_max_ratio", 0.75))
    cache = state.setdefault("comp_cache", {})
    ttl = timedelta(days=float(cfg.get("comp_cache_days", 7)))  # sold prices barely move week to week
    for k in [k for k, v in cache.items() if now - datetime.fromisoformat(v["at"]) > ttl]:
        del cache[k]
    ASK_CACHE.clear()
    ASK_CACHE.update(state.setdefault("ask_cache", {}))
    for k in [k for k, v in ASK_CACHE.items() if now - datetime.fromisoformat(v["at"]) > timedelta(hours=24)]:
        del ASK_CACHE[k]
    if cfg.get("precheck_priority") == "price":  # bigger cards first (more dollars per deal)
        state["queue"].sort(key=lambda q: q["price"] + q["shipping"], reverse=True)
    else:
        state["queue"].sort(key=lambda q: q["found_at"], reverse=True)  # newest first: real deals sell fast
    prechecked, ask_memo = 0, {}
    for q in state["queue"]:
        q.setdefault("query", build_query(q["title"], q["player"]))
        if "ask_ratio" in q or (q["query"] in cache and not ask_mode) or prechecked >= int(cfg.get("prechecks_per_run", 40)):
            continue
        try:
            q["ask_ratio"] = ask_ratio_cached(token, q, cfg, now)
        except Exception as e:  # noqa: BLE001
            log(f"pre-check failed: {e}")
            break
        prechecked += 1
    before = len(state["queue"])
    state["queue"] = [q for q in state["queue"] if q.get("ask_ratio") is None or q["ask_ratio"] <= ask_cutoff]
    log(f"pre-checked {prechecked} on eBay; dropped {before - len(state['queue'])} priced like everyone else")
    for a in auctions[:int(cfg.get("auction_prechecks_per_run", 20))]:
        if a["query"] in cache and not ask_mode:
            continue
        try:
            a["ask_ratio"] = ask_ratio_cached(token, a, cfg, now)
        except Exception as e:  # noqa: BLE001
            log(f"auction pre-check failed: {e}")
            break
    auctions = [a for a in auctions if a.get("ask_ratio") is None or a["ask_ratio"] <= ask_cutoff]

    if ask_mode:
        sent = ask_mode_alerts(state, cfg, env, now, auctions)
        log(f"done: {sent} deal alert(s) sent (comparing to other eBay listings)")
        save_state(state)
        return 0

    allowed = checks_allowed_this_run(state, cfg, now)
    sample_mode = os.environ.get("SAMPLE_ALERT") == "true"
    if sample_mode:
        allowed = max(allowed, 6)  # full test: price a few listings, send the best one as TEST
    best = None
    deals = 0
    log(f"CardSight checks allowed this run: {allowed} (used {state['usage']['calls']}/{cfg['monthly_budget']} this month)")

    def judge(listing: dict, mv: dict | None) -> None:
        nonlocal best, deals
        if listing.get("kind") == "auction":
            state["seen"][f"a:{listing['id']}"] = now.isoformat()  # judged once; don't alert twice
        if not mv:
            return
        if listing.get("kind") == "auction" and mv["median"] < float(cfg["min_price"]):
            return  # cheap card: not worth the time even at a discount
        ref = listing.get("ask_ref")
        if ref and mv["median"] > 1.6 * ref:
            log(f"  comps ${mv['median']:.0f} way above what sellers ask now (~${ref:.0f}); likely wrong card, skipping")
            return
        cost = listing["price"] + listing["shipping"]
        if best is None or 1 - cost / mv["median"] > best[0]:
            best = (1 - cost / mv["median"], listing, mv)
        log(f"${cost:.2f} vs ${mv['median']:.2f} ({mv['count']} comps): {listing['title'][:60]}")
        ev = evaluate(listing, mv, cfg)
        if ev:
            t, body = deal_message(listing, mv, ev, cfg)
            try:
                notify(env["NTFY_TOPIC"], t, body, url=listing["url"])
                deals += 1
            except Exception as e:  # noqa: BLE001
                log(f"notification failed: {e}")

    # auctions go first this run (they're about to end); they never stay in the saved queue
    state["queue"] = auctions + state["queue"]

    # 3a. listings of cards priced in the last 24h: judge for free
    rest = []
    for q in state["queue"]:
        if q["query"] in cache:
            judge(q, cache[q["query"]]["mv"])
        else:
            rest.append(q)
    state["queue"] = rest

    # 3b. spend CardSight checks on the most promising listings (cheapest vs other asks first)
    state["queue"].sort(key=lambda q: (q.get("kind") != "auction", q.get("ask_ratio") is None, q.get("ask_ratio") or 0))
    while allowed > 0 and state["queue"]:
        listing = state["queue"].pop(0)
        query = listing["query"]
        if query in cache:
            judge(listing, cache[query]["mv"])
            continue
        try:
            ratio = listing.get("ask_ratio")
            log(f"checking ({'n/a' if ratio is None else f'{ratio:.0%} of other asks'}): {listing['title'][:70]}  ->  q=\"{query}\"")
            data = cardsight_comps(env["CARDSIGHT_API_KEY"], query, cfg)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            log(f"CardSight error {status}: {body}")
            if status in (401, 403):
                error_alert(state, env["NTFY_TOPIC"], "CardSight rejected the API key. Check the CARDSIGHT_API_KEY secret.")
            spend(state)
            allowed -= 1
            if isinstance(status, int) and status >= 500:
                continue  # CardSight hiccup on this one listing: skip it, keep going
            if status == 429:
                state["queue"].insert(0, listing)
            break
        except Exception as e:  # noqa: BLE001
            log(f"CardSight request failed: {e}")
            state["queue"].insert(0, listing)
            break
        spend(state)
        allowed -= 1
        mv = market_value(listing["title"], data.get("results", []), cfg, listing.get("player", ""))
        cache[query] = {"mv": mv, "at": now.isoformat()}
        if not mv:
            log(f"no reliable comps: {listing['title'][:70]}")
        judge(listing, mv)
        time.sleep(0.3)

    state["queue"] = [q for q in state["queue"] if q.get("kind") != "auction"]

    if sample_mode:
        if best:
            disc, listing, mv = best
            cost = listing["price"] + listing["shipping"]
            ev = {"discount": disc, "profit": mv["median"] * (1 - float(cfg["fee_rate"])) - cost}
            t, body = deal_message(listing, mv, ev, cfg)
            notify(env["NTFY_TOPIC"], "TEST (not a deal) - " + t.replace(" under comps", " vs comps"), body,
                   url=listing["url"], priority="default", tags="test_tube")
            log(f"sent TEST alert: {t}")
        else:
            notify(env["NTFY_TOPIC"], "TEST - no listings could be priced", "Full test ran but nothing matched comps this time.",
                   priority="default", tags="test_tube")
            log("sent TEST alert: nothing priced")
    log(f"done: {deals} deal alert(s) sent")
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:  # noqa: BLE001
        import traceback
        log("CRASH: " + traceback.format_exc()[-1500:])
        try:
            st = load_state()
            save_state(st)  # keeps the crash message in state.json so it can be diagnosed
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
