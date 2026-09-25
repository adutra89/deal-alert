"""Dev tool: run CardSight pricing searches from probe/queries.txt, save trimmed results."""
import json, os, requests
key = os.environ["CARDSIGHT_API_KEY"]
out = []
for line in open("probe/queries.txt"):
    line = line.strip()
    if not line or line.startswith("#"):
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
