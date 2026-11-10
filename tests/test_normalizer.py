"""
Phase 2 tests: price, currency and unit parsing. Every input that the review found being silently
mis-read is covered here, alongside the formats the sample catalog relies on.
"""
from decimal import Decimal

import pytest

from app.services.normalizer import Normalizer, parse_decimal

normalizer = Normalizer()


# --- Plain numbers ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1250", "1250"),
    ("1,250", "1250"),               # comma + 3 digits = thousands
    ("1,500", "1500"),
    ("1,250,000", "1250000"),
    ("1,85,000.00", "185000.00"),    # Indian lakh grouping
    ("1,250.00", "1250.00"),
    ("2.450,50", "2450.50"),         # European
    ("1.250.000", "1250000"),        # repeated dots = thousands
    ("1'250.00", "1250.00"),         # Swiss
    ("12,5", "12.5"),                # comma + 1-2 digits = decimal
    ("450,50", "450.50"),
    ("0,500", "0.500"),              # leading zero: decimal, not thousands
    ("1.250", "1.250"),              # single dot = decimal (documented limitation)
    (".5", "0.5"),
    ("-450", "-450"),
    ("(450.00)", "-450.00"),         # accounting negative
])
def test_parse_decimal_accepts_real_number_formats(text, expected):
    assert parse_decimal(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["1e3", "1-2", "12abc", "1,2,3", "1,25,0", "", "--5", "1.2.3,4,5"])
def test_parse_decimal_rejects_anything_else(text):
    with pytest.raises(ValueError):
        parse_decimal(text)


# --- Prices ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, value, symbol", [
    ("$1,250.00", "1250.00", "$"),
    ("€ 2.450,50", "2450.50", "€"),
    ("€ 450,50", "450.50", "€"),
    ("₹ 1,85,000.00", "185000.00", "₹"),
    ("£ 850.00", "850.00", "£"),
    ("-$450.00", "-450.00", "$"),
    ("$-450.00", "-450.00", "$"),
    ("CA$ 99.00", "99.00", "CA$"),
    ("AU$1,000", "1000", "AU$"),
    ("Rs. 1,500", "1500", "RS."),
    ("USD 1,250.00", "1250.00", "USD"),
    ("1250 EUR", "1250", "EUR"),
    ("CHF 99", "99", "CHF"),
    ("0", "0", None),
    ("12,5", "12.5", None),
])
def test_parse_price_valid(raw, value, symbol):
    parsed, found_symbol, error = normalizer.parse_price(raw)
    assert error is None
    assert parsed == Decimal(value)
    assert found_symbol == symbol


@pytest.mark.parametrize("raw", ["N/A", "TBD", "1e3", "$1,250.00-ish", "Price: 1-2k", "approx 100", "$ 1,250 USD €"])
def test_parse_price_invalid_is_an_error_not_a_number(raw):
    parsed, _, error = normalizer.parse_price(raw)
    assert parsed is None
    assert "Invalid numeric price format" in error or "more than one currency marker" in error


def test_parse_price_missing():
    assert normalizer.parse_price(None) == (None, None, "Missing required unit_price value")
    assert normalizer.parse_price("   ")[2] == "Missing required unit_price value"


# --- Currency resolution ---------------------------------------------------------------------------

@pytest.mark.parametrize("column, symbol, expected", [
    (None, "€", "EUR"),
    (None, "₹", "INR"),
    (None, "RS.", "INR"),
    (None, "CA$", "CAD"),
    (None, "AU$", "AUD"),
    (None, "US$", "USD"),
    (None, "USD", "USD"),
    ("USD", "$", "USD"),     # bare '$' is resolved by an explicit currency column
    ("usd", None, "USD"),
    ("€", None, "EUR"),      # unambiguous symbol in the currency column
    ("EUR", "€", "EUR"),
])
def test_currency_resolves(column, symbol, expected):
    code, needs_review, reason = normalizer.resolve_currency(column, symbol)
    assert (code, needs_review, reason) == (expected, False, None)


@pytest.mark.parametrize("column, symbol, reason_part", [
    (None, "$", "Ambiguous currency symbol '$'"),
    (None, "¥", "Ambiguous currency symbol '¥'"),           # yen or yuan
    ("$", None, "Ambiguous currency symbol '$'"),
    ("USD", "€", "Currency conflict"),                       # price says euro, column says dollar
    ("EUR", "USD", "Currency conflict"),
    ("US Dollar", None, "not in supported list"),
    (None, "XYZ", "not in supported list"),
    (None, None, "Missing currency code"),
])
def test_currency_needs_review(column, symbol, reason_part):
    _, needs_review, reason = normalizer.resolve_currency(column, symbol)
    assert needs_review is True
    assert reason_part in reason


# --- Payload (kg) ----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("10 kg", 10.0),
    ("2.5kg", 2.5),
    ("1,500 kg", 1500.0),       # was 1.0
    ("500 g", 0.5),
    ("50 lbs", 22.68),
    ("12 lb", 5.443),
    ("5 tonnes", 5000.0),       # was 5.0
    ("16 oz", 0.454),
    ("-5 kg", -5.0),            # parsed; the Validator flags the range
])
def test_payload_conversion(raw, expected):
    value, _, issue = normalizer.normalize_payload_kg(raw)
    assert issue is None
    assert value == expected


def test_payload_conversion_note():
    value, note, issue = normalizer.normalize_payload_kg("500 g")
    assert value == 0.5
    assert issue is None
    assert "Converted 500.0 g -> 0.5 kg" in note


@pytest.mark.parametrize("raw, issue_part", [
    ("N/A", "Could not read payload value"),
    ("approx 10", "Could not read payload value"),
    ("10 stone", "Unknown payload unit 'stone'"),
    ("1500 mm", "Unknown payload unit 'mm'"),   # a length in the payload column
    ("10", "has no unit"),
])
def test_payload_unreadable_is_an_issue_not_a_guess(raw, issue_part):
    value, _, issue = normalizer.normalize_payload_kg(raw)
    assert value is None
    assert issue_part in issue


def test_payload_without_unit_uses_header_unit():
    value, note, issue = normalizer.normalize_payload_kg("10", header="Payload (lbs)")
    assert issue is None
    assert value == 4.536
    assert "from column header 'Payload (lbs)'" in note


def test_missing_payload_stays_null():
    assert normalizer.normalize_payload_kg(None) == (None, None, None)
    assert normalizer.normalize_payload_kg("  ") == (None, None, None)


# --- Reach (m) -------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1500 mm", 1.5),
    ("1,500 mm", 1.5),          # was 0.001
    ("1.2 m", 1.2),
    ("78 in", 1.981),
    ('78"', 1.981),
    ("6 ft", 1.829),
    ("120 cm", 1.2),
    ("2 km", 2000.0),           # was 2.0
])
def test_reach_conversion(raw, expected):
    value, _, issue = normalizer.normalize_reach_m(raw)
    assert issue is None
    assert value == expected


@pytest.mark.parametrize("raw, issue_part", [
    ("N/A", "Could not read reach value"),
    ("approx 1 m", "Could not read reach value"),
    ("3 yards", "Unknown reach unit 'yards'"),
    ("1500", "has no unit"),
])
def test_reach_unreadable_is_an_issue_not_a_guess(raw, issue_part):
    value, _, issue = normalizer.normalize_reach_m(raw)
    assert value is None
    assert issue_part in issue


def test_reach_without_unit_uses_header_unit():
    value, _, issue = normalizer.normalize_reach_m("1500", header="Reach [mm]")
    assert issue is None
    assert value == 1.5


def test_missing_reach_stays_null():
    assert normalizer.normalize_reach_m(None) == (None, None, None)
