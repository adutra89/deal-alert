# Karrd Kings deal alert

Every 20 minutes this checks new eBay Buy It Now listings for the players in `config.yml`, prices each one against recent **sold** auction comps from CardSight AI (matched on the exact card, parallel and grade), and pushes a notification to your phone (ntfy app) when price + shipping is at least 30% under market.

- **Change players, threshold, price range, budget:** edit `config.yml`.
- **Send a test notification:** Actions tab → Deal alert → Run workflow.
- **Pause it:** Actions tab → Deal alert → ⋯ → Disable workflow.

Secrets required (Settings → Secrets and variables → Actions): `EBAY_APP_ID`, `EBAY_CERT_ID`, `CARDSIGHT_API_KEY`, `NTFY_TOPIC`.

Always check the photos before buying. The alert finds candidates; you make the call.
