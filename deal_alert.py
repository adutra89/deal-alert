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


# ---------------------------------------------------------------- CardSight comps

BRANDS = [
    "topps", "bowman", "panini", "fleer", "upper deck", "donruss", "score", "leaf", "skybox", "hoops",
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


def card_number(title: str) -> str | None:
    m = CARD_NO_RE.search(title)
    if not m:
        return None
    return (m.group(1) or m.group(2)).upper().lstrip("0") or "0"


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
    cset = card.get("set") or {}
    release_words = [w for w in words(cset.get("release") or "") if len(w) > 1]
    if release_words and not all(w in tw for w in release_words):
        return False  # e.g. comp is "Topps Chrome Black" but listing is plain Topps
    set_name = (cset.get("name") or "").lower()
    if set_name and set_name not in ("base", "base set"):
        if not all(w in tw for w in words(set_name) if len(w) > 2 and w not in ("set", "cards")):
            return False  # insert set named in comp but not in listing
    pname = rec.get("parallel_name")
    if pname and not parallel_words_in_title(pname, listing_title):
        return False
    if not pname and looks_like_parallel(listing_title):
        return False  # listing looks like a parallel but comp is base
    return True


def market_value(listing_title: str, results: list[dict], cfg: dict) -> dict | None:
    """Find sold comps that are the same card, parallel and grade as the listing.

    Groups matching comps by exact identity and uses the biggest group. Returns None
    when nothing lines up or there are too few comps to trust.
    """
    title_grade = detect_grade(listing_title)
    title_num = card_number(listing_title)
    groups: dict[tuple, list[dict]] = {}
    for rec in results:
        if rec.get("listing_type", "auction") != "auction" or not rec.get("price"):
            continue
        if not identity_ok(rec, listing_title, title_grade, title_num):
            continue
        key = (rec["matched_card"]["card_id"], rec.get("parallel_id"), (rec.get("grade") or {}).get("grade_id"))
        groups.setdefault(key, []).append(rec)
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
    top = recs[0]
    card = top["matched_card"]
    cset = card.get("set") or {}
    label = " ".join(x for x in [
        str(cset.get("year") or ""), cset.get("release") or "",
        cset.get("name") if (cset.get("name") or "").lower() not in ("base", "base set") else "",
        card.get("name", ""), f"#{card['number']}" if card.get("number") else "", top.get("parallel_name") or "",
    ] if x)
    if title_grade:
        label += f" {title_grade[0]} {title_grade[1]}"
    prices = [float(r["price"]) for r in recs]
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
            state["queue"].append(it)
            new += 1
    log(f"{new} new listings queued, {len(state['queue'])} in queue")

    # 2. price the most valuable queued listings within the CardSight budget
    allowed = checks_allowed_this_run(state, cfg, now)
    sample_mode = os.environ.get("SAMPLE_ALERT") == "true"
    if sample_mode:
        allowed = max(allowed, 6)  # full test: price a few listings, send the best one as TEST
    best = None
    log(f"CardSight checks allowed this run: {allowed} (used {state['usage']['calls']}/{cfg['monthly_budget']} this month)")
    state["queue"].sort(key=lambda q: q["found_at"], reverse=True)  # newest first: real deals sell fast

    deals = 0
    while allowed > 0 and state["queue"]:
        listing = state["queue"].pop(0)
        try:
            query = build_query(listing["title"], listing["player"])
            log(f"checking: {listing['title'][:80]}  ->  q=\"{query}\"")
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
        if not mv:
            log(f"no reliable comps: {listing['title'][:70]}")
            continue
        ev = evaluate(listing, mv, cfg)
        cost = listing["price"] + listing["shipping"]
        if best is None or 1 - cost / mv["median"] > best[0]:
            best = (1 - cost / mv["median"], listing, mv)
        log(f"${listing['price'] + listing['shipping']:.2f} vs ${mv['median']:.2f} ({mv['count']} comps): {listing['title'][:60]}")
        if ev:
            t, body = deal_message(listing, mv, ev)
            try:
                notify(env["NTFY_TOPIC"], t, body, url=listing["url"])
                deals += 1
            except Exception as e:  # noqa: BLE001
                log(f"notification failed: {e}")
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
