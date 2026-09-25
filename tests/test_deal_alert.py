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
