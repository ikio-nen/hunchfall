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
# implemented form (see app/playtest/__main__.py):
python -m app.playtest --input app/playtest/samples/samples.jsonl --split test
python -m app.playtest --input app/playtest/samples/samples.jsonl --split tuning
#   --label REAL|SAMPLE  score one provenance only
#   --test-frac 0.3      held-out fraction handed to split.py
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

---

## 7. Round 1 results (2026-10-01) — first REAL set

**Provenance.** 20 `REAL` samples built by `backend/scripts/build_playtest_set.py`
from live first-party reads: resolved markets from Gamma `/markets?closed=true`,
the decision-time YES price from CLOB `/prices-history?market=<YES token>` (the
last traded price at or before **T-24h from resolution**), and the outcome from
Gamma `outcomePrices` on the closed market. Markets whose winner was ambiguous
(`["0","0"]`), whose history had no point before the cutoff, or whose
decision-time price was degenerate (< 0.02 or > 0.98) were **skipped, never
guessed**.

> **Known limitation — `news_snapshot` is not news.** This build had no
> historical news archive available (GDELT answered HTTP 429 throughout), so the
> field carries the market's **own Gamma metadata** (question, resolution
> criteria, resolution source) plus the decision-time price. Every sample's
> `source` says so. The set therefore validates the odds/outcome half of the
> pipeline and NOT the news half.

**Composition.** 20 REAL (6 YES / 14 NO, base rate 0.300) plus the 5 original
hand-written `SAMPLE` rows. The real rows are sports-heavy (soccer, tennis,
basketball, baseball) because `order=volume` on closed markets surfaces them.

### 7.1 The model column is MOCK, and it loses

The only decide_fn available offline is `mock_decide`, which derives P(true)
from `sha256(market_question)`. These numbers measure **a hash, not a model**.
They are printed because the honest answer to "does the agent beat the market
now?" is no, and hiding that would be worse.

| Split (REAL only) | n | Accuracy | Brier | Naive-baseline accuracy | Abstention |
|---|---|---|---|---|---|
| all | 20 | 0.2143 | 0.2636 | 0.700 | 0.300 |
| tuning | 14 | 0.1111 | 0.2950 | 0.786 | 0.357 |
| **test (held out)** | **6** | **0.4000** | **0.2071** | **0.500** | **0.167** |

Full set including `SAMPLE` rows (25): accuracy 0.2353, Brier 0.2439, baseline
0.720, abstention 0.320.

`mock_decide` trails the naive market-following baseline on accuracy in **every**
split (-0.486 all, -0.675 tuning, -0.100 test) and its Brier is worse than the
market's. Nothing graduates; no shadow-mode criterion is approached.

### 7.2 What the REAL data does say

The set is small, but its market prices behave like real prices — which is what
makes it usable as a yardstick:

| Split | n | Market Brier | 0.5-baseline | Market favourite accuracy |
|---|---|---|---|---|
| all | 20 | 0.1524 | 0.25 | 0.700 |
| tuning | 14 | 0.1475 | 0.25 | 0.786 |
| test | 6 | 0.1638 | 0.25 | 0.500 |

Market calibration over all 20 (10-bin):

| Bin | n | mean price | empirical YES |
|---|---|---|---|
| 0.0–0.1 | 5 | 0.055 | 0.000 |
| 0.1–0.2 | 1 | 0.170 | 0.000 |
| 0.3–0.4 | 1 | 0.300 | 0.000 |
| 0.4–0.5 | 3 | 0.468 | 0.333 |
| 0.5–0.6 | 9 | 0.506 | 0.444 |
| 0.7–0.8 | 1 | 0.795 | 1.000 |

The longshots resolved NO and the single 0.75 favourite resolved YES — the
dataset behaving as a real one should. The crowded 0.4–0.6 bins resolved YES
*less* often than priced, but with n=9 and n=3 that is noise, not a measured
favourite-longshot edge. **Do not tune on it.**

> **Caveat — 8 of the 20 rows sit at exactly 0.500.** These are sports
> spread/total props whose first quote *is* 0.500 and which had barely traded by
> T-24h, so the recorded price carries **no information**. The builder did not
> fabricate them: `price_at` only accepts a genuine print at or before the
> cutoff and skips the market otherwise (this is why many scanned markets are
> absent). Consequences to keep in mind when reading §7.1–7.2:
> * the model column gets a free coin flip on 40% of the set, and so does the
>   market-favourite baseline (it bets YES on `>= 0.5`, winning 3 of those 8);
> * the 0.4–0.6 calibration bins are mostly these uninformed rows, so the
>   apparent "fade the favourite" tilt in them is an artefact;
> * the real signal in this set lives in the price extremes (0.0–0.1 and 0.7+),
>   where both the market and the baseline behave sensibly.
>
> A future round should either drop `p == 0.500` rows or record the market's
> traded **volume** alongside the price so uninformed quotes are distinguishable
> from informed ones.

### 7.3 Verdict

1. **No model evidence exists yet.** Harness, split, baselines and REAL data are
   in place; the model is a hash. A real round needs `JEV_API_KEY` (or another
   decide_fn) wired into `run_round` — the harness prints `decide_fn_note`
   saying exactly this.
2. **The held-out split is 6 samples.** Section 6 wants ≥3 consecutive rounds
   beating baseline on the test set; at n=6 one sample moves test accuracy by
   ±0.17. The set must grow substantially before any graduation claim means
   anything.
3. **The naive baseline is strong** (0.700 all / 0.786 tuning — flattered by
   the eight uninformed 0.500 rows, see the caveat in §7.2). A future model has
   to beat *that*, not a coin flip.
4. Keep `SAMPLE` and `REAL` rows in separate reports (`--label`): the synthetic
   rows are hand-written and their 0.5-ish prices dilute the real distribution.

### 7.4 Bugs this milestone found (all fixed)

The live checks that produced this set exposed four product-breaking defects the
offline suite could not see, because the fixtures mirrored guessed shapes
(recorded in `docs/API_INVENTORY.md`):

1. `clobTokenIds` is a JSON-array string, not comma-separated — the parser
   returned ids wrapped in `["` … `"]` and CLOB answered `{"error": …}`.
2. `/book` levels are objects with string numbers, not `[price, size]` pairs —
   pair indexing raised `KeyError: 0`.
3. `/book` serves the **worst** quote first (bids ascending, asks descending) —
   index 0 gave a 50.0¢ mid and a 99.8¢ spread for a 2.85¢ market.
4. `/v2/trades?condition=…` interleaves **both** tokens — the tape's vwap read
   0.7628 for a 2.85¢ market and its "buy" flow was really the NO side's.

Round 1 numbers were produced **after** those fixes; the earlier data path could
not have produced a valid prediction at all.
