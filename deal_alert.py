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


def detect_grade(title: str) -> tuple[str, str] | None:
    """Return (company, grade) if the title says the card is slabbed."""
    m = GRADE_RE.search(title)
    if not m:
        return None
    return norm_grade(m.group(1), m.group(2))


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
    if burst_until and now < datetime.fromisoformat(str(burst_until)):
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
            "player": player,
            "found_at": datetime.now(timezone.utc).isoformat(),
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
        if detect_grade(t) != grade or (num and card_number(t) != num) or looks_like_parallel(t) != par:
            continue
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
    return (listing["price"] + listing["shipping"]) / low_quartile


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
]
CARD_NO_RE = re.compile(r"#\s*([A-Za-z]{0,6}-?\d{1,4}[A-Za-z]?)\b|\bno\.?\s*(\d{1,4})\b", re.I)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("'s", "s"))


CODE_NO_RE = re.compile(r"\b([A-Z]{1,6}-[A-Z]{0,3}\d{1,4}[A-Z]?)\b")


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
    parts.append(player)
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
]


def subsets_in(title: str) -> set[str]:
    t = " " + " ".join(words(title.replace("-", " "))) + " "
    found = {p for p in SUBSET_PHRASES if f" {p} " in t}
    # treat singular/plural variants as the same subset
    return {p.rstrip("s") for p in found}


def title_match_ok(rec_title: str, listing_title: str, title_grade, title_num) -> bool:
    """For sold comps CardSight couldn't tie to a catalog card: compare titles directly."""
    if not title_num or card_number(rec_title) != title_num:
        return False
    if detect_grade(rec_title) != title_grade:
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


def trim(prices) -> list[float]:
    """Drop sales far from the middle (mislabeled cards, shill bids, damaged copies)."""
    p = sorted(prices)
    if len(p) < 3:
        return p
    m = statistics.median(p)
    return [x for x in p if 0.5 * m <= x <= 2 * m]


def market_value(listing_title: str, results: list[dict], cfg: dict) -> dict | None:
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
    for rec in results:
        if rec.get("listing_type", "auction") != "auction" or not rec.get("price"):
            continue
        if rec.get("title") and subsets_in(rec["title"]) != listing_subsets:
            continue  # e.g. "Fresh Faces #3" ($450) is a different card from "All-Rookies #3" ($50)
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
    return {"median": statistics.median(prices), "count": len(prices), "label": label.strip()}


def evaluate(listing: dict, mv: dict, cfg: dict) -> dict | None:
    cost = listing["price"] + listing["shipping"]
    median = mv["median"]
    discount = 1 - cost / median
    if discount < float(cfg["discount_threshold"]):
        return None
    profit = median * (1 - float(cfg["fee_rate"])) - cost
    return {"cost": cost, "discount": discount, "profit": profit}


# ---------------------------------------------------------------- notifications

def notify(topic: str, title: str, body: str, url: str = "", priority: str = "high", tags: str = "moneybag") -> None:
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Open on eBay, {url}"
    r = requests.post(NTFY_URL + topic, data=body.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()


def deal_message(listing: dict, mv: dict, ev: dict) -> tuple[str, str]:
    title = f"{ev['discount']:.0%} under comps - {listing['player']}"
    body = (
        f"{listing['title']}\n\n"
        f"Price: ${listing['price']:,.2f} + ${listing['shipping']:,.2f} ship\n"
        f"Sold comps: ${mv['median']:,.2f} median ({mv['count']} sales)\n"
        f"Est. profit after fees: ${ev['profit']:,.2f}\n"
        f"Matched: {mv['label']}"
    )
    if ev["discount"] >= 0.70:
        body += "\n\nWARNING: 70%+ under market. Check photos closely for reprint, damage or wrong card."
    return title, body


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
            if player.split()[-1].lower() not in it["title"].lower():
                continue
            if title_has_excluded_word(it["title"], cfg["exclude_words"]):
                continue
            if not card_number(it["title"]):
                continue  # no card # in the title: too easy to price the wrong card
            state["queue"].append(it)
            new += 1
    log(f"{new} new listings queued, {len(state['queue'])} in queue")

    # 2. free pre-check: compare each new listing to other eBay asks for the same card
    ask_cutoff = float(cfg.get("precheck_max_ratio", 0.75))
    cache = state.setdefault("comp_cache", {})
    for k in [k for k, v in cache.items() if now - datetime.fromisoformat(v["at"]) > timedelta(hours=24)]:
        del cache[k]
    state["queue"].sort(key=lambda q: q["found_at"], reverse=True)  # newest first: real deals sell fast
    prechecked, ask_memo = 0, {}
    for q in state["queue"]:
        q.setdefault("query", build_query(q["title"], q["player"]))
        if "ask_ratio" in q or q["query"] in cache or prechecked >= int(cfg.get("prechecks_per_run", 40)):
            continue
        try:
            q["ask_ratio"] = ebay_ask_ratio(token, q, q["query"], cfg)
        except Exception as e:  # noqa: BLE001
            log(f"pre-check failed: {e}")
            break
        prechecked += 1
    before = len(state["queue"])
    state["queue"] = [q for q in state["queue"] if q.get("ask_ratio") is None or q["ask_ratio"] <= ask_cutoff]
    log(f"pre-checked {prechecked} on eBay; dropped {before - len(state['queue'])} priced like everyone else")

    allowed = checks_allowed_this_run(state, cfg, now)
    sample_mode = os.environ.get("SAMPLE_ALERT") == "true"
    if sample_mode:
        allowed = max(allowed, 6)  # full test: price a few listings, send the best one as TEST
    best = None
    deals = 0
    log(f"CardSight checks allowed this run: {allowed} (used {state['usage']['calls']}/{cfg['monthly_budget']} this month)")

    def judge(listing: dict, mv: dict | None) -> None:
        nonlocal best, deals
        if not mv:
            return
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
            t, body = deal_message(listing, mv, ev)
            try:
                notify(env["NTFY_TOPIC"], t, body, url=listing["url"])
                deals += 1
            except Exception as e:  # noqa: BLE001
                log(f"notification failed: {e}")

    # 3a. listings of cards priced in the last 24h: judge for free
    rest = []
    for q in state["queue"]:
        if q["query"] in cache:
            judge(q, cache[q["query"]]["mv"])
        else:
            rest.append(q)
    state["queue"] = rest

    # 3b. spend CardSight checks on the most promising listings (cheapest vs other asks first)
    state["queue"].sort(key=lambda q: (q.get("ask_ratio") is None, q.get("ask_ratio") or 0))
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
        mv = market_value(listing["title"], data.get("results", []), cfg)
        cache[query] = {"mv": mv, "at": now.isoformat()}
        if not mv:
            log(f"no reliable comps: {listing['title'][:70]}")
        judge(listing, mv)
        time.sleep(0.3)

    if sample_mode:
        if best:
            disc, listing, mv = best
            cost = listing["price"] + listing["shipping"]
            ev = {"discount": disc, "profit": mv["median"] * (1 - float(cfg["fee_rate"])) - cost}
            t, body = deal_message(listing, mv, ev)
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
    sys.exit(run())
