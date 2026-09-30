"""X (Twitter) source — EXCLUDED BY DESIGN. Do NOT implement.

X is deliberately not a story source for hunchfall:

* X's API is pay-per-use (~$0.005 per read at the tiers that matter) — too
  expensive for a hackathon paper-trading loop that polls every cycle.
* Scraping X without official API access violates X's Terms of Service and
  risks account/network bans for the whole team.

There is no fetch function here on purpose. The pipeline (``app/loop.py``)
never calls this module. If X access is ever reconsidered, it must go
through the official X API v2 with a paid tier and a fresh legal review —
not through this file.
"""
