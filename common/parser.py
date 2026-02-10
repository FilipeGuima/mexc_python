"""
Shared signal and update parsers for all trading bots.

SignalParser: Parses new trade signals (PAIR, SIDE, ENTRY, TP, SL, etc.)
UpdateParser: Parses update messages (change TP/SL)
parse_signal: Convenience wrapper with error handling.

NOTE: data['side'] returns plain strings "LONG" / "SHORT".
Each bot converts to its own enum/format at the call site.
"""

import re
import logging

logger = logging.getLogger(__name__)


class SignalParser:
    """
    Robust signal parser for NEW TRADES.
    Handles: "PAIR: BTC/USDT", "SIDE: LONG", "TP1: 0.55", ignoring "R:R" ratios.
    """
    NUM_PATTERN = r'([\d,]+\.?\d*)'

    # Hidden characters to strip from Telegram messages
    HIDDEN_CHARS = [
        '\u200b', '\u200c', '\u200d', '\u200e', '\u200f',
        '\u00a0', '\u2060', '\ufeff', '\u00ad', '\u2007',
        '\u2008', '\u2009', '\u200a', '\u202f', '\u205f', '\u3000',
    ]

    @staticmethod
    def _extract_number(text: str) -> float | None:
        if not text:
            return None
        cleaned = text.replace(',', '')
        try:
            return float(cleaned)
        except ValueError:
            return None

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Remove hidden unicode characters and normalize text."""
        for char in cls.HIDDEN_CHARS:
            text = text.replace(char, ' ')
        return text.upper()

    @classmethod
    def parse(cls, text: str, debug: bool = True) -> dict | None:
        logger.debug(f"RAW: {repr(text)}")

        text_upper = cls._clean_text(text)
        data = {}

        # Ignore Status Updates
        if "TARGET HIT" in text_upper or "PROFIT:" in text_upper:
            logger.debug("Ignored: Status/Profit update")
            return None

        # --- PAIR ---
        pair_match = re.search(r'PAIR[\W_]*([A-Z0-9]+)[\W_]*[/_:-]?[\W_]*([A-Z0-9]+)', text_upper)
        if not pair_match:
            logger.debug("Parsing failed: No PAIR found.")
            return None
        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}"

        # --- Check for Breakeven Command ---
        if "MOVE SL TO ENTRY" in text_upper:
            return {
                'type': 'BREAKEVEN',
                'symbol': data['symbol']
            }

        # --- SIDE (returns plain string) ---
        side_match = re.search(r'SIDE[\W_]*(LONG|SHORT)', text_upper)
        if not side_match:
            logger.debug("Parsing failed: No SIDE found.")
            return None
        data['side'] = side_match.group(1)  # "LONG" or "SHORT"

        # --- SIZE ---
        size_match = re.search(r'SIZE[\W_]*(\d+)[\W_]*(?:-[\W_]*(\d+))?[\W_]*%', text_upper)
        if size_match:
            val1 = float(size_match.group(1))
            val2 = float(size_match.group(2)) if size_match.group(2) else val1
            data['equity_perc'] = (val1 + val2) / 2
        else:
            data['equity_perc'] = 1.0

        # --- ENTRY ---
        entry_match = re.search(r'ENTRY[\W_]*' + cls.NUM_PATTERN + r'(?:[\W_]*-[\W_]*' + cls.NUM_PATTERN + r')?',
                                text_upper)
        if entry_match:
            entry1 = cls._extract_number(entry_match.group(1))
            entry2 = cls._extract_number(entry_match.group(2))
            if entry1 and entry2:
                data['entry'] = (entry1 + entry2) / 2
            elif entry1:
                data['entry'] = entry1
            else:
                data['entry'] = "Market"
        else:
            data['entry'] = "Market"

        # --- STOP LOSS ---
        sl_match = re.search(r'SL[\W_]*' + cls.NUM_PATTERN, text_upper)
        data['sl'] = cls._extract_number(sl_match.group(1)) if sl_match else None

        # --- LEVERAGE ---
        lev_match = re.search(r'LEV(?:ERAGE)?[\W_]*(\d+)', text_upper)
        data['leverage'] = int(lev_match.group(1)) if lev_match else 20

        # --- TAKE PROFIT TARGETS ---
        tp_matches = re.findall(r'TP\d[\W_]*' + cls.NUM_PATTERN + r'(?!\s*R:R)', text_upper)

        real_tps = []
        for tp_str in tp_matches:
            tp_val = cls._extract_number(tp_str)
            if tp_val:
                real_tps.append(tp_val)

        data['tps'] = real_tps[:3]
        data['type'] = 'TRADE'

        logger.debug(f"PARSED SIGNAL: {data}")
        return data


class UpdateParser:
    """
    Parses 'Update' messages like:
    "ASTER/USDT #1175 change TP1 to 0.75222"
    "BTC/USDT change SL to 94000"
    """

    @staticmethod
    def parse(text: str, debug: bool = True) -> dict | None:
        text_upper = text.upper()

        if not any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET", "UPDATE"]):
            return None

        data = {}

        pair_match = re.search(r'([A-Z0-9]+)[\W_]*[/_:-][\W_]*([A-Z0-9]+)', text_upper)
        if not pair_match:
            logger.debug("Update detected but NO PAIR found. Ignoring.")
            return None

        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}"

        sl_match = re.search(r'(?:SL|STOP(?:\s*LOSS)?)\W+(?:IS\W+)?(?:NOW|TO|BE)\W+([\d,.]+)', text_upper)
        if sl_match:
            data['type'] = 'SL'
            data['price'] = float(sl_match.group(1).replace(',', ''))
            logger.debug(f"PARSED UPDATE: {data}")
            return data

        tp_match = re.search(r'(TP\d?)\W+(?:IS\W+)?(?:NOW|TO|BE)\W+([\d,.]+)', text_upper)
        if tp_match:
            data['type'] = tp_match.group(1)
            data['price'] = float(tp_match.group(2).replace(',', ''))
            logger.debug(f"PARSED UPDATE: {data}")
            return data

        return None


def parse_signal(text: str) -> dict | None:
    """Wrapper for backward compatibility."""
    try:
        return SignalParser.parse(text, debug=True)
    except Exception as e:
        logger.error(f"Parse Error: {e}")
        return None
