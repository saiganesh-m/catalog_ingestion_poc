"""
Validator tests: parsed values and column-mapping results land in the right staging status.
Unreadable or out-of-range optional values go to review, unreadable prices are rejected, and an
unresolved column mapping sends rows to review instead of rejecting them for an "empty" value.
"""
import pytest

from app.services.normalizer import Normalizer
from app.services.validator import Validator

MAPPING = {
    "SKU": "sku", "Name": "name", "Price": "unit_price",
    "Currency": "currency", "Payload": "payload_kg", "Reach": "reach_m",
}
CLEAN_ROW = {"SKU": "X-1", "Name": "Widget", "Price": "$10.00", "Currency": "USD", "Payload": "5 kg", "Reach": "1 m"}


def classify(mapping=MAPPING, mapping_issues=(), unresolved=(), **changes):
    norm = Normalizer().normalize_row({**CLEAN_ROW, **changes}, mapping)
    return Validator().validate_and_classify(
        norm, excel_row_index=2, mapping_issues=mapping_issues, unresolved_required_fields=unresolved
    )


def test_clean_row_is_ready():
    outcome = classify()
    assert outcome.status == "ready"
    assert outcome.status_reason is None


@pytest.mark.parametrize("changes, field, expected", [
    ({"Payload": "1,500 kg"}, "payload_kg", 1500.0),
    ({"Payload": "5 tonnes"}, "payload_kg", 5000.0),
    ({"Reach": "1,500 mm"}, "reach_m", 1.5),
    ({"Currency": None, "Price": "CA$ 10.00"}, "currency", "CAD"),
    ({"Price": "$0.00"}, "unit_price", "0.00"),    # zero is allowed: price must be >= 0
])
def test_previously_misread_values_are_now_ready_and_correct(changes, field, expected):
    outcome = classify(**changes)
    assert outcome.status == "ready"
    assert outcome.normalized_dict[field] == expected


@pytest.mark.parametrize("changes, reason_part", [
    ({"Payload": "N/A"}, "Could not read payload value"),
    ({"Payload": "10 stone"}, "Unknown payload unit"),
    ({"Payload": "-5 kg"}, "payload_kg must be greater than 0"),
    ({"Reach": "0 m"}, "reach_m must be greater than 0"),
    ({"Reach": "1500"}, "has no unit"),
    ({"Currency": "USD", "Price": "€ 10.00"}, "Currency conflict"),
    ({"Currency": None, "Price": "¥ 1000"}, "Ambiguous currency symbol '¥'"),
])
def test_unresolved_values_need_review_with_a_reason(changes, reason_part):
    outcome = classify(**changes)
    assert outcome.status == "needs_review"
    assert reason_part in outcome.status_reason


@pytest.mark.parametrize("price", ["1e3", "$1,250.00-ish", "Price: 1-2k", "N/A", "-$5.00"])
def test_bad_price_is_rejected_even_when_every_column_is_confidently_mapped(price):
    outcome = classify(Price=price)
    assert outcome.status == "rejected"
    assert outcome.status_reason


def test_blank_value_in_a_mapped_required_column_is_rejected():
    outcome = classify(Name="")
    assert outcome.status == "rejected"
    assert "Missing required product name" in outcome.status_reason


def test_unresolved_required_column_sends_the_row_to_review_not_rejection():
    issue = "No confident column for 'name' (candidates: Item Label 0.55)"
    mapping = {header: field for header, field in MAPPING.items() if field != "name"}
    outcome = classify(mapping=mapping, mapping_issues=[issue], unresolved=["name"])
    assert outcome.status == "needs_review"
    assert issue in outcome.status_reason


def test_mapping_issue_keeps_an_otherwise_clean_row_out_of_ready():
    issue = "No confident column for 'payload_kg' (candidates: Weight 0.55/check 0.55)"
    outcome = classify(mapping_issues=[issue])
    assert outcome.status == "needs_review"
    assert outcome.status_reason == issue


def test_hard_failure_still_wins_over_an_unresolved_mapping():
    outcome = classify(Price="-$5.00", mapping_issues=["No confident column for 'name'"], unresolved=["name"])
    assert outcome.status == "rejected"
    assert "Price cannot be negative" in outcome.status_reason
