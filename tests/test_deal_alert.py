from datetime import datetime, timedelta, timezone

import deal_alert as d

CFG = {"min_comps": 3, "discount_threshold": 0.30, "fee_rate": 0.13, "monthly_budget": 700}


def rec(price, card="c1", parallel=None, pname=None, grade=None):
    r = {"price": price, "listing_type": "auction", "source": "ebay",
         "matched_card": {"card_id": card, "name": "Victor Wembanyama", "number": "136",
                          "set": {"name": "2023 Prizm"}},
         "parallel_id": parallel, "parallel_name": pname}
    if grade:
        r["grade"] = {"grade_id": f"g{grade}", "grade_value": grade, "company_name": "PSA", "company_id": "psa"}
    return r


def test_detect_grade():
    assert d.detect_grade("2023 Prizm Wembanyama #136 PSA 10 GEM MINT") == ("PSA", "10")
    assert d.detect_grade("Wembanyama BGS 9.5 Prizm") == ("BGS", "9.5")
    assert d.detect_grade("2023 Prizm Wembanyama #136 RC") is None


def test_raw_listing_uses_raw_comps_only():
    results = [rec(40, grade="10"), rec(10), rec(12), rec(11), rec(50, grade="10"), rec(45, grade="10")]
    mv = d.market_value("2023 Prizm Victor Wembanyama #136 Rookie", results, CFG)
    assert mv["median"] == 11 and mv["count"] == 3


def test_graded_listing_uses_matching_grade():
    results = [rec(10), rec(40, grade="10"), rec(50, grade="10"), rec(45, grade="10"), rec(20, grade="9")]
    mv = d.market_value("2023 Prizm Wembanyama #136 PSA 10", results, CFG)
    assert mv["median"] == 45


def test_parallel_listing_skips_base_comps():
    results = [rec(10), rec(12), rec(11)]
    assert d.market_value("2023 Prizm Wembanyama #136 Silver Prizm RC", results, CFG) is None
    assert d.market_value("2023 Prizm Wembanyama #136 /99", results, CFG) is None


def test_parallel_match():
    results = [rec(60, parallel="p1", pname="Silver"), rec(70, parallel="p1", pname="Silver"),
               rec(65, parallel="p1", pname="Silver"), rec(10)]
    mv = d.market_value("2023 Prizm Wembanyama #136 Silver Prizm RC", results, CFG)
    assert mv["median"] == 65


def test_too_few_comps():
    assert d.market_value("2023 Prizm Wembanyama #136", [rec(10), rec(11)], CFG) is None


def test_evaluate_threshold():
    listing = {"price": 60, "shipping": 5}
    assert d.evaluate(listing, {"median": 100}, CFG)["discount"] == 0.35
    assert d.evaluate({"price": 70, "shipping": 5}, {"median": 100}, CFG) is None


def test_excluded_words():
    words = ["lot", "you pick", "rp"]
    assert d.title_has_excluded_word("Michael Jordan 10 card LOT", words)
    assert d.title_has_excluded_word("Jordan Fleer RP rookie", words)
    assert not d.title_has_excluded_word("Michael Jordan 1986 Fleer #57 PSA 8", words)


def test_budget_spreads_evenly():
    state = {"usage": {"month": "", "calls": 0, "credit": 0.0}}
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    total = 0
    runs = d.runs_left_in_month(now)
    for i in range(runs):
        t = now + timedelta(minutes=20 * i)
        n = d.checks_allowed_this_run(state, CFG, t)
        d.spend(state, n)
        total += n
    assert 650 <= total <= 700


def test_build_query_strips_filler():
    q = d.build_query("1990 Fleer - Michael Jordan #26 Chicago Bulls HOF PSA 9 🔥", "Michael Jordan")
    assert q == "1990 fleer Michael Jordan 26 PSA 9"


def test_release_must_match_listing():
    r = rec(200, pname=None)
    r["matched_card"]["set"] = {"name": "Base Set", "release": "Topps Chrome Black", "year": "2026"}
    assert not d.identity_ok(r, "2026 Topps Shohei Ohtani #136", None, "136")
    r["matched_card"]["set"]["release"] = "Topps"
    assert d.identity_ok(r, "2026 Topps Shohei Ohtani #136", None, "136")


def test_card_number_must_match():
    r = rec(10)
    assert not d.identity_ok(r, "2023 Prizm Wembanyama #275", None, "275")


def test_unmatched_comps_by_title():
    res = [{"price": p, "listing_type": "auction", "title": t} for p, t in [
        (40, "1996-97 Fleer Metal Kobe Bryant #137 RC PSA 8"),
        (44, "1996 FLEER METAL #137 KOBE BRYANT ROOKIE PSA 8 LAKERS"),
        (38, "Kobe Bryant 1996 Metal Fleer #137 PSA 8"),
        (90, "1996 Fleer Metal Kobe Bryant #137 PSA 9"),
        (15, "1996 Fleer Kobe Bryant #203 PSA 8"),
    ]]
    mv = d.market_value("1996-97 Fleer Metal - Fresh Foundation Kobe Bryant #137 (RC) - PSA 8", res, CFG)
    assert mv["median"] == 40 and mv["count"] == 3


def test_release_word_squashed():
    r = rec(10)
    r["matched_card"]["number"] = "324"
    r["matched_card"]["set"] = {"name": "Base Set", "release": "Topps Project70", "year": "2021"}
    assert d.identity_ok(r, "2021 Topps Project 70 Shohei Ohtani by DJ Skee #324", None, "324")


def test_no_card_number_is_not_priced():
    # real miss: Clutch Gene insert #CG-17 got priced as base #221
    r = [rec(70, grade="9"), rec(71, grade="9"), rec(72, grade="9")]
    for x in r:
        x["matched_card"]["number"] = "221"
    assert d.market_value("Victor Wembanyama PSA 9 Clutch Gene 2025-26 Topps Chrome Basketball", r, CFG) is None
    assert d.card_number("2025-26 Topps Chrome - Clutch Gene Victor Wembanyama #CG-17- PSA 9") == "CG-17"


def test_subset_names_must_agree():
    # real miss: All-Rookies #3 (~$50) was valued using Fresh Faces #3 sales (~$450)
    res = [{"price": p, "listing_type": "auction", "title": t} for p, t in [
        (450, "1996-97 Fleer Ultra Fresh Face Kobe Bryant #3 RC"),
        (495, "KOBE BRYANT 1996 FLEER ULTRA #3 ROOKIE FRESH FACES RC LAKERS"),
        (433, "1996 Fleer Ultra Kobe Bryant Fresh Faces Rookie RC #3 Lakers"),
        (49, "1996-97 Fleer Ultra All-Rookie Kobe Bryant #3 RC"),
        (61, "KOBE BRYANT FLEER ULTRA 1996-97 ALL ROOKIE INSERT #3"),
        (42, "1996-97 Fleer Ultra All Rookie Kobe Bryant #3 Rookie Insert"),
    ]]
    mv = d.market_value("Fleer Ultra 1996-97 All-Rookies Kobe Bryant Lakers #3 Rookie", res, CFG)
    assert mv["median"] == 49


def test_trim_outliers():
    assert d.trim([40, 45, 50, 400]) == [40, 45, 50]


def test_auction_message_has_max_bid():
    from datetime import datetime, timedelta, timezone
    a = {"kind": "auction", "title": "t", "player": "Kobe Bryant", "price": 20.0, "shipping": 5.0, "bids": 1,
         "ends_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
    t, body = d.deal_message(a, {"median": 100.0, "count": 6, "label": "x"}, {}, {"discount_threshold": 0.35})
    assert "AUCTION" in t and "Bid up to $60.00" in body


def test_graded_sale_titles_excluded_for_raw_listing():
    # real miss: raw 97-98 Metal Universe Jordan #23 valued at $395 using graded sales CardSight left untagged
    res = []
    for p, t in [(400, "1997-98 Metal Universe Michael Jordan #23 PSA 10"), (390, "97 Metal Universe Jordan #23 BGS 9.5"),
                 (395, "1997 Metal Universe #23 Michael Jordan PSA 10 GEM MINT"), (40, "1997-98 Metal Universe Michael Jordan #23"),
                 (45, "Michael Jordan 1997 Metal Universe #23 Bulls"), (38, "1997-98 Skybox Metal Universe Jordan #23")]:
        r = rec(p); r["title"] = t; r["matched_card"]["number"] = "23"
        r["matched_card"]["set"] = {"name": "Base Set", "release": "Metal Universe", "year": "1997-98"}
        res.append(r)
    mv = d.market_value("1997-98 Skybox Metal Universe Michael Jordan #23", res, CFG)
    assert mv["median"] == 40


def test_year_must_match():
    r = rec(3); r["matched_card"]["number"] = "453"
    r["matched_card"]["set"] = {"name": "Base Set", "release": "Upper Deck", "year": "1992-93"}
    assert not d.identity_ok(r, "1988 Upper Deck Michael Jordan #453", None, "453")


def test_retro_variation_not_priced_as_base():
    # real miss: 2008-09 Topps Kobe #24 1958-59 Variation (~$15) valued at $398 using the base #24 (w/ LeBron)
    res = []
    for p, t in [(398, "2008-09 Topps Kobe Bryant #24 Lakers"), (406, "2008-09 Topps Kobe Bryant #24 HOF"),
                 (375, "2008-09 Topps Kobe Bryant #24 Lakers"), (13, "2008-09 Topps #24 Kobe Bryant 1958-59 Variations"),
                 (16.5, "2008-09 Topps Kobe Bryant #24 1958-59 Variations LA Lakers"), (12, "2008-09 Topps #24 Kobe Bryant 1958-59 Variations"),
                 (11, "Topps 2008-09 Kobe Bryant #24 1958-59 Variations")]:
        r = rec(p); r["title"] = t; r["matched_card"]["number"] = "24"
        r["matched_card"]["set"] = {"name": "Base Set", "release": "Topps", "year": "2008-09"}
        res.append(r)
    mv = d.market_value("2008-09 Topps - Kobe Bryant #24 1958-59 Variations", res, CFG)
    assert mv["median"] == 12.5
    assert d.subsets_in("Fleer Ultra All-Rookies Kobe #3") == d.subsets_in("Ultra All Rookie Kobe #3")


def test_player_rules_keep_dads_out():
    cfg = {"player_rules": {"Ken Griffey Jr": {"exclude": ["sr", "sr.", "senior"], "min_year": 1987},
                            "Bobby Witt Jr": {"exclude": ["sr", "senior"], "min_year": 2019}}}
    assert not d.player_rule_ok("1976 Topps Ken Griffey #400 Reds PSA 7", "Ken Griffey Jr", cfg)
    assert not d.player_rule_ok("1991 Topps Ken Griffey Sr. #100 Mariners", "Ken Griffey Jr", cfg)
    assert d.player_rule_ok("1989 Upper Deck Ken Griffey Jr. #1 RC PSA 8", "Ken Griffey Jr", cfg)
    assert not d.player_rule_ok("1990 Topps Bobby Witt #19 Rangers", "Bobby Witt Jr", cfg)
    assert d.player_rule_ok("2022 Topps Chrome Bobby Witt Jr. #USC50 RC", "Bobby Witt Jr", cfg)


def test_sub_product_must_match():
    # real miss: 2008-09 Topps Co-Signers LeBron #23 (~$3) valued at $274 using base 2008-09 Topps #23
    res = []
    for p, t in [(270, "2008-09 Topps LeBron James #23 Cavaliers"), (280, "2008 Topps LeBron James #23"),
                 (274, "2008-09 Topps #23 LeBron James"), (3, "2008-09 Topps Co-Signers LeBron James #23"),
                 (3.5, "2008-09 Topps Co-Signers - LeBron #23"), (2.3, "2008-09 Topps Co Signers LeBron James #23"),
                 (5.4, "2008-09 Topps Co-Signers LeBron James #23")]:
        r = rec(p); r["title"] = t; r["matched_card"]["number"] = "23"
        r["matched_card"]["set"] = {"name": "Base Set", "release": "Topps", "year": "2008-09"}
        res.append(r)
    mv = d.market_value("2008-09 Topps Co-Signers - LeBron James #23", res, CFG, "LeBron James")
    assert mv["median"] == 3.25
    assert d.sub_products("2008 Topps Chrome Kobe #24") != d.sub_products("2008 Topps Kobe #24")


def test_insert_named_in_sale_not_in_listing():
    # real miss: 1993-94 Fleer Ultra All-NBA Team Jordan #2 PSA 6 valued at $510 using "Power in the Key #2" sales
    res = [{"price": p, "listing_type": "auction", "title": t} for p, t in [
        (510, "MICHAEL JORDAN PSA 6 1993-94 FLEER ULTRA #2 POWER IN THE KEY"),
        (495, "1993 Fleer Ultra Power In The Key Michael Jordan #2 PSA 6"),
        (530, "1993-94 Ultra Power in the Key #2 Michael Jordan PSA 6"),
        (60, "1993-94 Fleer Ultra All-NBA Team Michael Jordan #2 PSA 6"),
        (55, "1993 Fleer Ultra All NBA Michael Jordan #2 PSA 6"),
        (65, "1993-94 Fleer Ultra All-NBA Jordan #2 PSA 6 Bulls"),
    ]]
    mv = d.market_value("1993-94 Fleer Ultra All-NBA Team Michael Jordan #2 PSA 6 HOF 00r7", res, CFG, "Michael Jordan")
    assert mv["median"] == 60


def test_members_only_and_finest_are_different_versions():
    # real miss: 1996 Topps Stars Jordan #24 PSA 9 compared with Members Only / Finest asks and sold links
    base = "1996 Topps Stars - Michael Jordan #24 PSA 9"
    for other in ["1996 Topps Stars Michael Jordan #24 Members Only PSA 9", "1996 Topps Stars Finest #24 Jordan PSA 9"]:
        assert d.subsets_in(other) != d.subsets_in(base) or d.sub_products(other) != d.sub_products(base)
    l = {"title": base, "player": "Michael Jordan"}
    l["query"] = d.build_query(base, "Michael Jordan")
    url = d.sold_search_url(l)
    assert "_sacat=261328" in url and "-finest" not in url


def test_raw_vs_graded():
    assert d.detect_grade("2002 Bowman Chrome Tom Brady #99 Graded 8 by SGC") == ("SGC", "8")
    assert d.is_graded("2007-08 Topps Chrome LeBron James #23", "Ungraded") is False
    assert d.is_graded("2007-08 Topps Chrome LeBron James #23 PSA ready", "") is False
    assert d.is_graded("2007-08 Topps Chrome LeBron James #23 graded slab", "") is None
    assert d.is_graded("Wemby #221 Topps Chrome", "Graded") is True
    assert not d.grade_clear({"title": "Wemby #221 Topps Chrome", "condition": "Graded"})  # graded but grade unreadable
    # raw listing must not use a sale that's described as graded
    r = rec(120); r["title"] = "2007-08 Topps Chrome LeBron James #23 Graded"; r["matched_card"]["number"] = "23"
    r["matched_card"]["set"] = {"name": "Base Set", "release": "Topps Chrome", "year": "2007-08"}
    assert not d.identity_ok(r, "2007-08 Topps Chrome - LeBron James #23", None, "23")


def test_candidates_without_asks_do_not_crash():
    # real crash: auctions with no comparable listings had no ask_ref (Oct 7-9)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    a = {"id": "A9", "kind": "auction", "title": "1996 Topps Kobe Bryant #138 PSA 8", "player": "Kobe Bryant",
         "price": 20.0, "shipping": 5.0, "bids": 1, "ends_at": (now + timedelta(minutes=10)).isoformat(),
         "url": "u", "ask_ratio": None, "query": "q-none"}
    state = {"queue": [], "seen": {}, "usage": {"month": now.strftime("%Y-%m"), "calls": 0}}
    cfg = {"ask_discount": 0.1, "allow_no_asks": True, "min_price": 50, "monthly_budget": 0,
           "verify_with_cardsight": True, "require_sold_confirmation": True, "max_alerts_per_run": 3}
    assert d.ask_mode_alerts(state, cfg, {"NTFY_TOPIC": "t", "CARDSIGHT_API_KEY": "k"}, now, [a]) == 0
