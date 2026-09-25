"""Dev tool: run lines from probe/queries.txt against CardSight, save trimmed results.

Lines: "<path> <params>" for raw calls, or "TITLE <player> | <ebay title>" to run
the real deal_alert matching on a listing title.
"""
import json, os, sys
sys.path.insert(0, ".")
import requests
import deal_alert as d

key = os.environ["CARDSIGHT_API_KEY"]
cfg = d.load_config()
out = []
for line in open("probe/queries.txt"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("TITLE "):
        player, _, title = line[6:].partition(" | ")
        q = d.build_query(title, player)
        d.RUN_LOG.clear()
        data = d.cardsight_comps(key, q, cfg)
        res = data.get("results", [])
        mv = d.market_value(title, res, cfg)
        out.append({"title": title, "query": q, "n_results": len(res), "market_value": mv, "log": list(d.RUN_LOG),
                    "top": [{k: r.get(k) for k in ("title", "price", "parallel_name", "matched_card", "grade")} for r in res[:5]]})
        continue
    path, _, q = line.partition(" ")
    params = dict(p.split("=", 1) for p in q.split("&")) if "=" in q else {"q": q}
    r = requests.get(f"https://api.cardsight.ai{path}", headers={"X-API-Key": key}, params=params, timeout=60)
    try:
        body = r.json()
    except Exception:
        body = r.text[:500]
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        body["results"] = body["results"][:6]
    out.append({"path": path, "params": params, "status": r.status_code, "body": body})
json.dump(out, open("probe/results.json", "w"), indent=1)
