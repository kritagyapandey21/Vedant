"""
Signal analysis module (v3-reversion-plus, see signal_v3.py and implement.md).
Turns the engine's oscillator scores into setup strength and the final signal JSON.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
import signal_v3
from config import MIN_CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)


class SignalAnalyzer:
    """Analyze market data and generate trading signals."""

    @staticmethod
    def analyze_pair(pair_data: Dict) -> Optional[Dict]:
        """
        Analyze a single pair with the v3-reversion-plus engine.

        Args:
            pair_data: Dictionary containing closes, symbol and current_price

        Returns:
            Dictionary with analysis results, or None when the pair has no clean setup
        """
        closes = pair_data["closes"]
        symbol = pair_data["symbol"]

        if len(closes) < signal_v3.MIN_CANDLES:
            logger.warning(f"Insufficient data for {symbol}: {len(closes)} candles (need {signal_v3.MIN_CANDLES})")
            return None

        result = signal_v3.analyze(closes)
        if result is None:
            return None

        d = result["direction"]
        comp = result["components"]

        def agree(x: float) -> float:
            """0-100 reading of one score: above 50 supports the call."""
            return round(max(0.0, min(100.0, 50 + 50 * x * d)), 1)

        osc_rsi, osc_bb, osc_sr = agree(comp["rsi_mr"]), agree(comp["bb_mr"]), agree(comp["sr_mr"])
        # Average of the three readings that decided the call: how stretched the price is, not a win probability.
        confidence = round(min(99.0, (osc_rsi + osc_bb + osc_sr) / 3), 2)

        return {
            "symbol": symbol,
            "confidence": confidence,
            "direction": "CALL" if d > 0 else "PUT",
            "price": pair_data["current_price"],
            "signals": {
                "osc_rsi": osc_rsi,
                "osc_bollinger": osc_bb,
                "osc_stoch_rsi": osc_sr,
                "rsi": agree(result["score"]),
                "ema": agree(result["ema_stack"]),
                "macd": agree(result["macd"]),
                "supertrend": agree(result["supertrend"]["score"]),
                "strength": round(result["ker"] * 100, 1),
            }
        }

    @staticmethod
    def select_best_signal(
        all_analyses: List[Dict],
        min_confidence: float = MIN_CONFIDENCE_THRESHOLD
    ) -> Optional[Dict]:
        """
        Select the pair with highest confidence that meets minimum threshold.
        
        Args:
            all_analyses: List of analysis results for each pair
            min_confidence: Minimum confidence threshold
            
        Returns:
            Best signal analysis or None if no pair meets threshold
        """
        if not all_analyses:
            return None
        
        # Filter by minimum confidence
        qualified = [a for a in all_analyses if a["confidence"] >= min_confidence]
        
        if not qualified:
            logger.warning(f"No pairs met minimum confidence threshold ({min_confidence}%)")
            return None
        
        # Sort by confidence and return highest
        return max(qualified, key=lambda x: x["confidence"])

    @staticmethod
    def generate_signal(
        best_analysis: Dict,
        entry_time: datetime,
        expiry_time: datetime
    ) -> Dict:
        """
        Generate final trading signal with timing information.
        
        Args:
            best_analysis: Analysis data for selected pair
            entry_time: Signal entry time
            expiry_time: Signal expiry time
            
        Returns:
            Complete signal JSON
        """
        signal = {
            "pair": best_analysis["symbol"],
            "direction": "BUY" if best_analysis["direction"] == "CALL" else "SELL",
            "entry_time": entry_time.strftime("%H:%M:%S"),
            "expiry_time": expiry_time.strftime("%H:%M:%S"),
            "entry_timestamp": entry_time.isoformat(),
            "expiry_timestamp": expiry_time.isoformat(),
            "confidence": round(best_analysis["confidence"], 1),
            "current_price": best_analysis["price"],
            "indicator_scores": best_analysis["signals"],
            "generated_at": datetime.now(IST).strftime("%H:%M:%S"),
            "generated_timestamp": datetime.now(IST).isoformat(),
            "candle_duration": "1 minute",
            "engine": signal_v3.ENGINE,
        }
        
        return signal
