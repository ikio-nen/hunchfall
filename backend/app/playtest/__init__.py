"""Offline playtesting harness: labeled examples, metrics, reports.

Validation is playtesting-first: strategies are scored on labeled examples
(news snapshot + odds snapshot + known outcome) before anything paper-trades.
Samples are synthetic and labeled SAMPLE; real labeled data is labeled REAL.
"""
