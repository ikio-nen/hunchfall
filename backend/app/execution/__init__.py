"""Paper execution engine.

Simulated fills against live CLOB touch prices. No orders ever leave the
machine: fills are modeled as taker fills that cross the spread, with
explicit fee and slippage accounting. Every fill is written to the audit log.
"""

from app.execution.paper import Fill, PaperEngine, Position, mark_to_market

__all__ = ["Fill", "PaperEngine", "Position", "mark_to_market"]
