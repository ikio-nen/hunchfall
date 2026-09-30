# hunchfall — Playtesting

Validation is **playtesting-first**: we prove the agent on resolved history before
it ever touches a live paper loop. No live claim is made until rounds pass here.

## 1. Building the labeled set

Each sample = one real, resolved Polymarket market:

- **News snapshot** — the story/momentum context as it looked *before* resolution
  (from scraper-style sources: Reddit/News/GDELT).
- **Odds snapshot** — the CLOB book state (YES price, spread, depth, volume) at the
  same pre-resolution timestamp.
- **Resolved outcome** — the actual YES/NO resolution from Polymarket.
- **`source` field** — `REAL` for genuine resolved-market samples, `SAMPLE` for
  synthetic ones. This field is never stripped; every report shows it.

Schema lives in `app/playtest/schema.py`. Real sets are JSONL, one market per line,
following that schema.

## 2. Tuning / calibration / held-out test split

- Split the labeled set **60 / 20 / 20, time-based**: train on the *older* 60%,
  calibrate on the *middle* 20%, test on the *newest* 20%. Time-based ordering
  is mandatory — a random split leaks future information into tuning and
  overstates edge. Never tune on test.
- Thresholds (`EDGE_THRESHOLD`, `KELLY_FRACTION`, spread/depth/skew filters)
  may only be adjusted against the train split.
- **Calibration split: fix Jev overconfidence here, not on test.** Fit
  temperature scaling (single temperature T on the noul logits) or Platt
  scaling on the calibration split, then freeze the parameters. Below the
  reliability diagonal = overconfident (Jev's known failure mode) — shrink
  `KELLY_FRACTION` and widen abstention on train/calibration evidence only.
- Test set is run once per round for the official numbers. If you retune, the
  old test numbers are void — re-run fresh.
- **Favorite-longshot bias (FLB): re-measure, don't hardcode.** Measure the
  current FLB direction on 2025–2026 resolved data at tuning time; never bake
  a fixed bias correction into the gate.

## 3. Round protocol

```
python -m app.playtest run --split test --round N
```

Per round, `run_round` reports:

| Metric | What it means |
|---|---|
| Accuracy | fraction of non-abstained predictions matching resolved outcome |
| Abstention rate | fraction of samples the agent declined to trade |
| Brier score | mean squared error of P(true) vs outcome (lower = better calibrated) |
| Calibration bins | accuracy vs predicted probability per bin (see reliability diagram) |

**Baseline comparison:** every round is scored against the **naive market-following
baseline** (predict whatever the market price says, same abstention rules). The
agent graduates only if it beats naive market-following on the held-out test set
on accuracy *and* Brier, with a sane abstention rate (it must abstain when unsure,
not trade everything).

## 4. How to run

- **Synthetic smoke test** (5 built-in `SAMPLE` markets, clearly labeled):
  ```
  python -m app.playtest --samples
  ```
- **Real labeled set** (JSONL per `app/playtest/schema.py`):
  ```
  python -m app.playtest --file data/labeled.jsonl --split tuning   # tune thresholds
  python -m app.playtest --file data/labeled.jsonl --split test     # official round
  ```
- **With mock Jev** (no network, outputs labeled MOCK): set `JEV_MOCK=1`.

## 5. Interpreting the reliability diagram

The harness plots predicted-probability bins (x) against actual YES frequency (y):

- **On the diagonal** = well calibrated. Bets sized on these probabilities are honest.
- **Above the diagonal** = underconfident (markets resolve YES more often than
  predicted) — consider whether the edge threshold is too conservative.
- **Below the diagonal** = overconfident — shrink `KELLY_FRACTION`, widen
  abstention, do NOT "fix" it by tuning on test.
- **Empty bins** = the agent never predicts there; that is fine, it just means
  no evidence about calibration in that range.

## 6. Shadow-mode graduation criteria

The agent may enter shadow mode (`python -m app.loop --watch`, paper only, kill
switch armed) only when:

1. >= 3 consecutive rounds on the **held-out test set** beat the naive
   market-following baseline on accuracy and Brier.
2. Abstention rate is stable and non-trivial (the agent demonstrably says "no").
3. Reliability diagram shows no severe miscalibration in the traded bins.
4. All veto paths exercised at least once in playtest (news/price skew,
   wide spread, thin book, negrisk invalid, uma dispute, low Jev confidence,
   model skip, jev error, near resolution, exposure cap, max positions).
5. Kill-switch drills pass: manual `POST /kill` halts the loop; simulated
   drawdown >= `KILL_DRAWDOWN_PCT` triggers auto-kill.

Shadow mode = live data, **paper fills only**, full veto/audit logging. Any
criterion violated in shadow mode sends it back to playtesting.
