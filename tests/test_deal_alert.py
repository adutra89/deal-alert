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
