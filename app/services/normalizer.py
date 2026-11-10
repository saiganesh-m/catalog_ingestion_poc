import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings


@dataclass
class NormalizedRowResult:
    """Holds deterministic normalization results for a single catalog row."""
    sku: Optional[str] = None
    name: Optional[str] = None
    unit_price: Optional[Decimal] = None
    currency: Optional[str] = None
    payload_kg: Optional[float] = None
    reach_m: Optional[float] = None

    is_currency_ambiguous: bool = False
    currency_ambiguity_reason: Optional[str] = None
    normalization_errors: List[str] = field(default_factory=list)   # unreadable required values (price)
    measurement_issues: List[str] = field(default_factory=list)     # unreadable optional values (payload, reach)
    normalization_notes: List[str] = field(default_factory=list)    # conversions applied


TARGET_FIELDS = ("sku", "name", "unit_price", "currency", "payload_kg", "reach_m")

# Price cells that are explicitly "no price"
INVALID_PLACEHOLDERS = {"N/A", "NA", "TBD", "TBA", "UNKNOWN", "FREE", "CALL", "POA", "NONE", "-", "--"}

# Currency marker (upper-case) -> ISO codes it can stand for. More than one code means it is ambiguous.
CURRENCY_SYMBOLS: Dict[str, Tuple[str, ...]] = {
    "US$": ("USD",),
    "CA$": ("CAD",),
    "C$": ("CAD",),
    "AU$": ("AUD",),
    "A$": ("AUD",),
    "S$": ("SGD",),
    "$": ("USD", "CAD", "AUD", "SGD"),
    "€": ("EUR",),
    "£": ("GBP",),
    "₹": ("INR",),
    "RS.": ("INR",),
    "RS": ("INR",),
    "¥": ("JPY", "CNY"),
}

_SYMBOL_PATTERN = re.compile(
    "|".join(re.escape(s) for s in sorted(CURRENCY_SYMBOLS, key=len, reverse=True) if not s.startswith("RS"))
    + r"|\bRs\.?(?=\s*[\d(-])",
    re.IGNORECASE,
)
_ISO_CODE_PATTERN = re.compile(r"\b[A-Za-z]{3}\b")
_MEASUREMENT_PATTERN = re.compile(r"(?P<number>[-+−(]?[\d.,'\s]*\d[\d.,'\s]*\)?)\s*(?P<unit>[^\d\s].*)?")
_HEADER_UNIT_PATTERN = re.compile(r"[(\[]\s*([^)\]]+?)\s*[)\]]")


def _unit_aliases(factor: str, *names: str) -> Dict[str, Decimal]:
    return {name: Decimal(factor) for name in names}


# Unit (lower-case) -> factor to the target unit
MASS_UNITS_TO_KG: Dict[str, Decimal] = {
    **_unit_aliases("1", "kg", "kgs", "kilogram", "kilograms"),
    **_unit_aliases("0.001", "g", "gram", "grams"),
    **_unit_aliases("1000", "t", "tonne", "tonnes"),
    **_unit_aliases("0.45359237", "lb", "lbs", "pound", "pounds"),
    **_unit_aliases("0.028349523125", "oz", "ounce", "ounces"),
}
LENGTH_UNITS_TO_M: Dict[str, Decimal] = {
    **_unit_aliases("1", "m", "metre", "metres", "meter", "meters"),
    **_unit_aliases("0.001", "mm", "millimetre", "millimetres", "millimeter", "millimeters"),
    **_unit_aliases("0.01", "cm", "centimetre", "centimetres", "centimeter", "centimeters"),
    **_unit_aliases("1000", "km", "kilometre", "kilometres", "kilometer", "kilometers"),
    **_unit_aliases("0.0254", "in", "inch", "inches", '"', "″"),
    **_unit_aliases("0.3048", "ft", "foot", "feet"),
}


def _clean_spaces(text: str) -> str:
    """Normalise non-breaking and thin spaces (common in exported spreadsheets) to plain spaces."""
    return text.replace(" ", " ").replace(" ", " ").replace(" ", " ").strip()


def _strip_thousands(integer_part: str) -> str:
    """Remove thousands separators from the integer part, rejecting malformed grouping like '1,2,3'."""
    separators = {sep for sep in ",." if sep in integer_part}
    if not separators:
        return integer_part
    if len(separators) > 1:
        raise ValueError("mixed thousands separators")
    groups = integer_part.split(separators.pop())
    first, rest = groups[0], groups[1:]
    western = all(len(group) == 3 for group in rest)                              # 1,250,000
    indian = len(rest[-1]) == 3 and all(len(group) == 2 for group in rest[:-1])  # 1,85,000
    if not (1 <= len(first) <= 3 and (western or indian)):
        raise ValueError("malformed thousands grouping")
    return "".join(groups)


def parse_decimal(text: str) -> Decimal:
    """
    Parse a plain number written in US/UK, European, Swiss or Indian style. Raises ValueError on anything else.

    Separator rules:
      both ',' and '.'  -> the last one is the decimal separator   (1,250.00 / 2.450,50)
      only ','          -> thousands if a 3-digit group follows     (1,500 / 1,85,000), else decimal (12,5 / 450,50)
      only '.'          -> thousands if repeated (1.250.000), else decimal (1.250 = 1.25)
      "'"               -> thousands (Swiss: 1'250.00)
      leading '-' or (parentheses) -> negative
    """
    s = re.sub(r"\s+", "", text)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s[:1] in ("-", "−"):
        negative, s = True, s[1:]
    elif s[:1] == "+":
        s = s[1:]
    if not re.fullmatch(r"[\d.,']*\d[\d.,']*", s):
        raise ValueError("not a plain number")

    s = s.replace("'", "")
    if "," in s and "." in s:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        integer_part, _, fraction = s.rpartition(",")
        is_thousands = s.count(",") > 1 or (len(fraction) == 3 and integer_part.lstrip("0") != "")
        decimal_sep = None if is_thousands else ","
    elif s.count(".") > 1:
        decimal_sep = None
    else:
        decimal_sep = "." if "." in s else None

    if decimal_sep:
        integer_part, _, fraction = s.rpartition(decimal_sep)
        if not fraction.isdigit():
            raise ValueError("misplaced decimal separator")
    else:
        integer_part, fraction = s, ""

    digits = _strip_thousands(integer_part) or "0"
    try:
        value = Decimal(f"{digits}.{fraction}" if fraction else digits)
    except InvalidOperation:
        raise ValueError("not a plain number")
    return -value if negative else value


class Normalizer:
    """
    Deterministic data transformation engine.
    Converts prices, resolves currencies, normalizes measurement units to standard SI units (m, kg),
    and preserves missing optional values as NULL. Anything it cannot read exactly is reported, never guessed.
    """

    def normalize_row(
        self,
        raw_row_data: Dict[str, Any],
        column_mappings: Dict[str, str],
    ) -> NormalizedRowResult:
        """
        Transforms raw supplier key-values into standardized schema candidate.
        Uses JEV column mappings to extract target fields.
        """
        result = NormalizedRowResult()

        # Step 1: Invert column mappings (target_field -> supplier_column_name).
        # Columns marked "irrelevant" or "unmapped" are never read; JEV settles at most one column per field.
        field_to_col: Dict[str, str] = {}
        for col_name, target_field in column_mappings.items():
            if target_field in TARGET_FIELDS and target_field not in field_to_col:
                field_to_col[target_field] = col_name

        def raw(target: str) -> Any:
            return raw_row_data.get(field_to_col.get(target, ""), None)

        # 1. Normalize SKU & Name
        raw_sku, raw_name = raw("sku"), raw("name")
        result.sku = str(raw_sku).strip() if raw_sku is not None and str(raw_sku).strip() else None
        result.name = str(raw_name).strip() if raw_name is not None and str(raw_name).strip() else None

        # 2. Normalize Price & Currency Marker
        parsed_price, price_symbol, price_err = self.parse_price(raw("unit_price"))
        result.unit_price = parsed_price
        if price_err:
            result.normalization_errors.append(price_err)

        # 3. Resolve Currency & Check Ambiguity
        curr_code, is_ambig, ambig_reason = self.resolve_currency(raw("currency"), price_symbol)
        result.currency = curr_code
        result.is_currency_ambiguous = is_ambig
        result.currency_ambiguity_reason = ambig_reason

        # 4. Normalize Payload (to kilograms) and Reach (to metres)
        for target, normalize in (("payload_kg", self.normalize_payload_kg), ("reach_m", self.normalize_reach_m)):
            value, note, issue = normalize(raw(target), header=field_to_col.get(target))
            setattr(result, target, value)
            if note:
                result.normalization_notes.append(note)
            if issue:
                result.measurement_issues.append(issue)

        return result

    def parse_price(self, raw_val: Any) -> Tuple[Optional[Decimal], Optional[str], Optional[str]]:
        """
        Parses a formatted price into (Decimal, currency marker, error).
        The marker is a symbol ('$', '€', 'CA$') or an ISO code ('USD') found in the cell, or None.
        After removing the marker, the rest must be a plain number; '1e3', '1-2k' or '1,250-ish' are errors.
        """
        if raw_val is None or str(raw_val).strip() == "":
            return None, None, "Missing required unit_price value"

        text = _clean_spaces(str(raw_val))
        if text.upper() in INVALID_PLACEHOLDERS:
            return None, None, f"Invalid numeric price format: '{text}'"

        markers: List[str] = []

        def take_symbol(match: re.Match) -> str:
            markers.append(match.group(0).upper())
            return " "

        def take_iso_code(match: re.Match) -> str:
            token = match.group(0)
            if token.isupper() or token.upper() in settings.supported_currency_list:
                markers.append(token.upper())
                return " "
            return token  # ordinary text such as 'ish' stays and makes the number invalid

        remainder = _ISO_CODE_PATTERN.sub(take_iso_code, _SYMBOL_PATTERN.sub(take_symbol, text))

        distinct_markers = sorted(set(markers))
        if len(distinct_markers) > 1:
            return None, None, f"Price '{text}' contains more than one currency marker: {distinct_markers}"
        symbol = distinct_markers[0] if distinct_markers else None

        try:
            return parse_decimal(remainder), symbol, None
        except ValueError as exc:
            return None, symbol, f"Invalid numeric price format: '{text}' ({exc})"

    def resolve_currency(
        self,
        explicit_col: Optional[str],
        embedded_symbol: Optional[str],
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """
        Resolves the currency code as (code, needs_review, reason). Enforces the assignment rule:
        "Do not silently interpret an ambiguous currency symbol. Use explicit source context or send the record for review."
        """
        supported = settings.supported_currency_list
        price_codes = self._codes_for_marker(embedded_symbol)

        # Case A: Explicit currency column (an ISO code, or an unambiguous symbol such as '€')
        if explicit_col is not None and str(explicit_col).strip():
            column_value = str(explicit_col).strip()
            column_codes = self._codes_for_marker(column_value)
            if len(column_codes) > 1:
                return None, True, (
                    f"Ambiguous currency symbol '{column_value}' in the currency column "
                    f"(could be {', '.join(column_codes)})"
                )
            code = column_codes[0] if column_codes else column_value.upper()
            if code not in supported:
                return code, True, f"Explicit currency '{column_value}' is not in supported list ({settings.SUPPORTED_CURRENCIES})"
            if price_codes and code not in price_codes:
                return code, True, (
                    f"Currency conflict: the price shows '{embedded_symbol}' but the currency column says '{code}'"
                )
            return code, False, None

        # Case B: Marker inside the price cell
        if len(price_codes) == 1:
            code = price_codes[0]
            if code in supported:
                return code, False, None
            return code, True, f"Currency '{code}' is not in supported list ({settings.SUPPORTED_CURRENCIES})"

        # Case C: Ambiguous symbol such as a bare '$' or '¥'
        if len(price_codes) > 1:
            return None, True, (
                f"Ambiguous currency symbol '{embedded_symbol}' without an explicit currency column "
                f"(could be {', '.join(price_codes)})"
            )

        # Case D: No currency information at all
        return None, True, "Missing currency code and currency symbol in source record"

    @staticmethod
    def _codes_for_marker(marker: Optional[str]) -> Tuple[str, ...]:
        """ISO codes a currency marker can stand for: '€' -> (EUR,), '$' -> (USD, CAD, ...), 'usd' -> (USD,)."""
        if not marker:
            return ()
        key = marker.strip().upper()
        if key in CURRENCY_SYMBOLS:
            return CURRENCY_SYMBOLS[key]
        if re.fullmatch(r"[A-Z]{3}", key):
            return (key,)
        return ()

    def normalize_payload_kg(
        self, raw_val: Any, header: Optional[str] = None
    ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        """Normalizes a payload cell to kilograms. Returns (value, conversion note, issue)."""
        return self._parse_measurement(raw_val, header, MASS_UNITS_TO_KG, "kg", "payload")

    def normalize_reach_m(
        self, raw_val: Any, header: Optional[str] = None
    ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        """Normalizes a reach cell to metres. Returns (value, conversion note, issue)."""
        return self._parse_measurement(raw_val, header, LENGTH_UNITS_TO_M, "m", "reach")

    @staticmethod
    def _parse_measurement(
        raw_val: Any,
        header: Optional[str],
        units: Dict[str, Decimal],
        base_unit: str,
        quantity: str,
    ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        """
        Read '<number> <unit>' and convert it to the base unit. Missing values stay None (no issue).
        A cell without a unit uses the unit from the column header, e.g. 'Payload (kg)'; otherwise it is an issue.
        Unknown units are an issue, never assumed. Range checks belong to the Validator.
        """
        if raw_val is None or str(raw_val).strip() == "":
            return None, None, None

        text = _clean_spaces(str(raw_val))
        unreadable = f"Could not read {quantity} value '{text}' (expected a number with a unit, e.g. '10 {base_unit}')"
        match = _MEASUREMENT_PATTERN.fullmatch(text)
        if not match:
            return None, None, unreadable
        try:
            number = parse_decimal(match.group("number"))
        except ValueError:
            return None, None, unreadable

        unit = (match.group("unit") or "").strip().lower().rstrip(".")
        note: Optional[str] = None
        if not unit:
            header_match = _HEADER_UNIT_PATTERN.search(header or "")
            header_unit = header_match.group(1).strip().lower() if header_match else None
            if header_unit not in units:
                return None, None, f"{quantity.capitalize()} value '{text}' has no unit and the column header does not state one"
            unit = header_unit
            note = f"No unit in {quantity} cell '{text}'; used '{unit}' from column header '{header}'"

        if unit not in units:
            return None, None, f"Unknown {quantity} unit '{unit}' in '{text}'"

        value = float((number * units[unit]).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
        if units[unit] != 1:
            conversion = f"Converted {float(number)} {unit} -> {value} {base_unit}"
            note = f"{note}; {conversion}" if note else conversion
        return value, note, None
