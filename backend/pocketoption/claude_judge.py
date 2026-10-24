"""Claude review of the shortlisted signal candidates.

After RSI/EMA/MACD vote and rank the pairs, the top few candidates are sent to Claude, which picks the
cleanest setup or rejects them all when the market looks choppy or exhausted. It judges setup quality only
and cannot predict the market.

Needs ANTHROPIC_API_KEY. Without a key (or if the call fails) review() returns None and the plain
indicator ranking is used, so the app keeps working.

  CLAUDE_JUDGE_MODEL   default claude-opus-5-5 (set claude-haiku-5-5 for a faster, cheaper review)
  CLAUDE_JUDGE_TIMEOUT seconds, default 25
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "claude-opus-5-5")
TIMEOUT = float(os.environ.get("CLAUDE_JUDGE_TIMEOUT", "25"))

SYSTEM = (
    "You are a cautious reviewer for a one minute signal tool. You receive up to three shortlisted candidates, "
    "already ranked by how stretched the price is (RSI, Bollinger %B and Stochastic RSI). Each is a mean-reversion "
    "fade: CALL expects a bounce up after a drop, PUT expects a pullback after a rise. Pick the single cleanest "
    "setup, or reject all of them if the recent price action is a steady trend that is unlikely to snap back, "
    "or looks choppy. You judge setup quality only "
    "and cannot predict the market. Reply with JSON only, no other text: "
    '{"pick": <candidate index or null>, "reason": "<at most 25 words>"}'
)


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _describe(index: int, c: Dict) -> str:
    closes = c.get("closes", [])[-30:]
    series = ", ".join(f"{x:.5g}" for x in closes)
    s = c.get("signals", {})
    return (
        f"[{index}] {c['symbol']} direction={c['direction']} confidence={c['confidence']} "
        f"rsi={s.get('osc_rsi')} bollinger={s.get('osc_bollinger')} stoch_rsi={s.get('osc_stoch_rsi')} "
        f"trend_strength={s.get('strength')}\n   last closes (oldest to newest): {series}"
    )


async def _call(prompt: str) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(timeout=TIMEOUT, max_retries=1)
    response = await client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


async def review(candidates: List[Dict]) -> Optional[Dict]:
    """Returns {"pick": int | None, "reason": str}, or None when the review is unavailable."""
    if not candidates or not enabled():
        return None
    prompt = "Candidates:\n" + "\n".join(_describe(i, c) for i, c in enumerate(candidates))
    try:
        text = await _call(prompt)
        match = re.search(r"\{.*\}", text, re.S)
        data = json.loads(match.group(0))
        pick = data.get("pick")
        if pick is not None:
            pick = int(pick)
            if not 0 <= pick < len(candidates):
                raise ValueError("pick out of range")
        return {"pick": pick, "reason": str(data.get("reason", "")).strip()[:240]}
    except Exception as e:                                   # never let the review break signal delivery
        logger.warning("Claude review unavailable (%s): using the indicator ranking", e)
        return None
