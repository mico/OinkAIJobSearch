"""Historical API cost estimates in micro-USD.

Codex uses a ChatGPT subscription, not these legacy per-token API rates.
Unknown/default models have no attributable dollar estimate (stored as zero);
the operator surface explicitly labels subscription billing as unavailable.
"""
from __future__ import annotations

from typing import Optional


# Historical char-to-token conversion retained for pre-cutover model records.
_CHARS_PER_TOKEN = 4.0

# Legacy rates only; never apply these to a Codex/default model.
_PRICES = {
    "haiku":  {"in": 1.00,  "out": 5.00},
    "sonnet": {"in": 3.00,  "out": 15.00},
    "opus":   {"in": 15.00, "out": 75.00},
    None:     {"in": 0.0, "out": 0.0},
}


def estimate_cost_us(
    model: Optional[str],
    prompt_chars: int,
    output_chars: int,
) -> int:
    """Return a micro-USD cost estimate for one CLI call.

    micro-USD = USD * 1_000_000. So a $0.000123 call returns 123. We
    round to int so storage stays compact and SUM()s exact.

    Unknown/default models return zero: subscription dollar billing cannot
    be derived from token counts.
    """
    rates = _PRICES.get(model, _PRICES[None])
    p_tok = max(0, int(prompt_chars)) / _CHARS_PER_TOKEN
    o_tok = max(0, int(output_chars)) / _CHARS_PER_TOKEN
    # Per-Mtok rates → per-token = rate / 1e6 USD = rate USD per Mtok.
    # cost_usd = (p_tok * in_rate + o_tok * out_rate) / 1_000_000
    # cost_us  = cost_usd * 1_000_000 = p_tok * in_rate + o_tok * out_rate
    cost_us = p_tok * rates["in"] + o_tok * rates["out"]
    return int(round(cost_us))
