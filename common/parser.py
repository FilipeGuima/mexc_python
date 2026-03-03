"""
Shared signal and update parsers for all trading bots.

BaseSignalParser: Abstract base for pluggable signal parsers.
DefaultSignalParser: Wraps existing SignalParser (PAIR/SIDE format).
BinanceKillersParser: Parses Binance Killers format (COIN/Direction).
SignalParser: Parses new trade signals (PAIR, SIDE, ENTRY, TP, SL, etc.)
UpdateParser: Parses update messages (change TP/SL)
parse_signal: Convenience wrapper with error handling.

NOTE: data['side'] returns plain strings "LONG" / "SHORT".
Each bot converts to its own enum/format at the call site.
"""

import re
import logging
import unicodedata
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ===================================================================
# PLUGGABLE PARSER ABSTRACTION
# ===================================================================

class BaseSignalParser(ABC):
    """Abstract base class for signal parsers. Allows strategies to use different message formats."""

    @abstractmethod
    def can_handle(self, text: str) -> bool:
        """Return True if this parser recognizes the message format."""
        pass

    @abstractmethod
    def parse(self, text: str) -> dict | None:
        """Parse the message text and return a standard signal dict, or None on failure."""
        pass


class DefaultSignalParser(BaseSignalParser):
    """Wraps the existing SignalParser (PAIR/SIDE format)."""

    def can_handle(self, text: str) -> bool:
        text_upper = text.upper()
        return "PAIR" in text_upper and "SIDE" in text_upper

    def parse(self, text: str) -> dict | None:
        return parse_signal(text)


class BinanceKillersParser(BaseSignalParser):
    """
    Parser for Binance Killers signal format.

    Example message:
        COIN: $DOT/USDT (2-5x)
        Direction: LONG
        ENTRY: 1.500 - 1.510
        TARGETS: 1.575 - 1.675 - 1.800 - 1.950 - 2.100
        STOP LOSS: 1.375
    """

    @staticmethod
    def _parse_decimal(text: str) -> float | None:
        """Parse a number where '.' is ALWAYS a decimal point (never thousands).
        Crypto prices like 1.500 must stay 1.5, not become 1500."""
        if not text:
            return None
        cleaned = re.sub(r'[^\d.]', '', text)
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def can_handle(self, text: str) -> bool:
        text_upper = text.upper()
        return "COIN" in text_upper and "DIRECTION" in text_upper

    def parse(self, text: str) -> dict | None:
        try:
            return self._parse_inner(text)
        except Exception as e:
            logger.error(f"BinanceKillersParser error: {e}")
            return None

    def _parse_inner(self, text: str) -> dict | None:
        # Clean invisible unicode characters
        text_clean = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]', '', text)
        text_upper = text_clean.upper()

        # Ignore status updates
        if "TARGET HIT" in text_upper or "PROFIT:" in text_upper:
            return None

        # --- COIN (symbol) ---
        # Matches: COIN: $DOT/USDT  or  COIN: DOT/USDT  or  COIN : DOT_USDT
        coin_match = re.search(
            r'COIN\s*[:]\s*\$?\s*([A-Z0-9]+)\s*[/_]\s*([A-Z0-9]+)',
            text_upper
        )
        if not coin_match:
            logger.debug("BK parse: No COIN found.")
            return None
        symbol = f"{coin_match.group(1)}_{coin_match.group(2)}"

        # --- LEVERAGE (from COIN line, e.g. "(2-5x)" or "(10x)") ---
        lev_match = re.search(r'\((\d+)\s*-\s*(\d+)\s*[xX]\)', text_clean)
        if lev_match:
            lev1 = int(lev_match.group(1))
            lev2 = int(lev_match.group(2))
            leverage = (lev1 + lev2) // 2
        else:
            lev_single = re.search(r'\((\d+)\s*[xX]\)', text_clean)
            leverage = int(lev_single.group(1)) if lev_single else 20

        # --- DIRECTION (side) ---
        dir_match = re.search(r'DIRECTION\s*[:]\s*(LONG|SHORT)', text_upper)
        if not dir_match:
            logger.debug("BK parse: No DIRECTION found.")
            return None
        side = dir_match.group(1)

        # --- ENTRY ---
        entry_match = re.search(
            r'ENTRY\s*[:]\s*([\d.,]+)\s*(?:-\s*([\d.,]+))?',
            text_upper
        )
        if entry_match:
            e1 = self._parse_decimal(entry_match.group(1))
            e2 = self._parse_decimal(entry_match.group(2))
            if e1 and e2:
                entry = (e1 + e2) / 2
            elif e1:
                entry = e1
            else:
                entry = "Market"
        else:
            entry = "Market"

        # --- TARGETS (dash-separated TP list) ---
        targets_match = re.search(
            r'TARGETS?\s*[:]\s*(.+)',
            text_upper
        )
        tps = []
        if targets_match:
            targets_line = targets_match.group(1)
            # Split by dash, comma, or whitespace-dash-whitespace
            tp_parts = re.split(r'\s*[-,]\s*', targets_line.strip())
            for part in tp_parts:
                val = self._parse_decimal(part.strip())
                if val:
                    tps.append(val)

        # --- STOP LOSS ---
        sl_match = re.search(r'STOP\s*LOSS\s*[:]\s*([\d.,]+)', text_upper)
        sl = self._parse_decimal(sl_match.group(1)) if sl_match else None

        if not tps:
            logger.debug("BK parse: No TARGETS found.")
            return None

        return {
            'type': 'TRADE',
            'symbol': symbol,
            'side': side,
            'entry': entry,
            'tps': tps[:3],
            'sl': sl,
            'leverage': leverage,
            'equity_perc': 2.0,
        }


class SignalParser:
    """
    Robust signal parser for NEW TRADES.
    Handles: "PAIR: BTC/USDT", "SIDE: LONG", "TP1: 0.55", ignoring "R:R" ratios.
    """
    # Matches numbers with commas, periods, or mixed separators
    # e.g., "67,000", "67.000", "67,000.50", "0.55", "95000"
    NUM_PATTERN = r'([\d.,]+)'

    @staticmethod
    def _extract_number(text: str) -> float | None:
        if not text:
            return None
        # Strip any non-numeric chars that might have slipped through
        cleaned = re.sub(r'[^\d.,]', '', text)
        if not cleaned:
            return None

        # Detect thousands separators vs decimal points:
        # "67.000" or "67,000" → 67000 (thousands sep if 3 digits after separator)
        # "0.55" or "67,000.50" → keep decimal point
        # Strategy: find the LAST separator — if followed by exactly 3 digits at end, it's thousands
        parts_dot = cleaned.split('.')
        parts_comma = cleaned.split(',')

        if '.' in cleaned and ',' in cleaned:
            # Mixed: "67,000.50" or "67.000,50" — last separator is decimal
            # Whichever appears last is the decimal separator
            last_dot = cleaned.rfind('.')
            last_comma = cleaned.rfind(',')
            if last_dot > last_comma:
                # Period is decimal: "67,000.50" → remove commas, keep last dot
                cleaned = cleaned.replace(',', '')
            else:
                # Comma is decimal: "67.000,50" → remove dots, comma→dot
                cleaned = cleaned.replace('.', '').replace(',', '.')
        elif ',' in cleaned:
            # Only commas: "67,000" → thousands separator
            cleaned = cleaned.replace(',', '')
        elif '.' in cleaned:
            # Only periods: could be decimal ("0.55") or thousands ("67.000")
            if re.match(r'^\d{1,3}(\.\d{3})+$', cleaned):
                # Pattern like "67.000" or "1.000.000" → thousands separator
                cleaned = cleaned.replace('.', '')
            # else: "0.55", "67.5" → decimal point, keep as is

        try:
            return float(cleaned)
        except ValueError:
            return None

    # Characters that are meaningful for signal parsing — everything else is stripped.
    # Letters and digits are always kept (handled separately).
    _KEEP_CHARS = set(' \n\r\t.,:/%-#()@')

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Whitelist-based text cleaning for signal parsing.

        Only keeps characters that matter for signal extraction:
        - Letters (A-Z) and digits (0-9) — always kept
        - Meaningful punctuation: . , : / % - # ( ) @
        - Whitespace: space, newline, tab

        Everything else is stripped: markdown formatting (* _ ~ `),
        emojis, invisible Unicode, control chars, decorative symbols, etc.

        NFKC normalization runs first to convert fullwidth/special chars
        to their ASCII equivalents before filtering.
        """
        text = unicodedata.normalize('NFKC', text)

        cleaned = []
        for ch in text:
            if ch.isalnum() or ch in cls._KEEP_CHARS:
                cleaned.append(ch)
        return ''.join(cleaned).upper()

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
        logger.debug(f"TP raw matches: {tp_matches}")

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
