"""
JEV integration tests: request format, the two-round decision (choice, then yes/no checks), per-field
resolution, failure handling per JEV_MODE, and evidence capture. The live API is simulated with
httpx.MockTransport in the documented System One format, so no network calls are made.
"""
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.services.jev import JevApiError, JevClient, JevService
from app.services.jev.service import TARGET_OPTIONS

API_KEY = "test-secret-key-123"
HEADERS = [
    "Part Number", "Product Description", "Unit Cost", "Extended Amount",
    "Rated Load", "Working Reach", "Currency", "Warranty Duration",
]
# header -> (JEV choice, confidence). "Extended Amount" deliberately competes with "Unit Cost".
CHOICES = {
    "Part Number": ("sku", 1.0), "Product Description": ("name", 1.0),
    "Unit Cost": ("unit_price", 0.95), "Extended Amount": ("unit_price", 0.85),
    "Rated Load": ("payload_kg", 0.9), "Working Reach": ("reach_m", 1.0),
    "Currency": ("currency", 1.0), "Warranty Duration": ("irrelevant", 1.0),
    "Item Label": ("name", 1.0),
}
# (header, candidate field) -> yes-probability of the follow-up check
CHECKS = {
    ("Unit Cost", "unit_price"): 0.97, ("Extended Amount", "unit_price"): 0.03,
    ("Rated Load", "payload_kg"): 0.95, ("Working Reach", "reach_m"): 0.96,
    ("Product Description", "name"): 0.9, ("Item Label", "name"): 0.9,
}


def fake_jev(choices=None, checks=None, requests=None):
    """Answer like POST /v1/systemone, from the given choice and check tables."""
    choices = {**CHOICES, **(choices or {})}
    checks = {**CHECKS, **(checks or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        body = json.loads(request.content)
        answers = {}
        for question_id, question in body["questions"].items():
            header = question["instructions"]["column"]["header"]
            if question["type"] == "choice":
                choice, confidence = choices[header]
                probabilities = {option: 0.0 for option in question["criteria"]}
                probabilities[choice] = confidence
                probabilities["name" if choice == "irrelevant" else "irrelevant"] = round(1 - confidence, 4)
                answers[question_id] = {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}
            else:
                candidate = question["instructions"]["candidate_field"]["name"]
                answers[question_id] = {"type": "noul", "noul": checks[(header, candidate)]}
        return httpx.Response(
            200,
            json={"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 300, "output_tokens": 40}},
            headers={"x-typesafe-request-id": f"req_test_{len(requests or [])}"},
        )

    return handler


def make_service(monkeypatch, handler, mode="auto") -> JevService:
    monkeypatch.setattr(settings, "JEV_MODE", mode)
    monkeypatch.setattr(settings, "JEV_API_KEY", API_KEY)
    client = JevClient(transport=httpx.MockTransport(handler))
    client.backoff_initial = 0
    return JevService(client=client)


def map_columns(monkeypatch, headers=HEADERS, mode="auto", **fake_kwargs):
    service = make_service(monkeypatch, fake_jev(**fake_kwargs), mode)
    return service.map_columns(headers, {header: [] for header in headers})


# --- Request format --------------------------------------------------------------------------------

def test_two_rounds_match_the_documented_request_format(monkeypatch):
    requests = []
    map_columns(monkeypatch, requests=requests)

    assert len(requests) == 2
    for request in requests:
        assert str(request.url).endswith("/v1/systemone")
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"

    mapping, follow_up = (json.loads(request.content) for request in requests)
    assert mapping["model"] == settings.JEV_MODEL
    assert [column["header"] for column in mapping["state"]["columns"]] == HEADERS
    assert all(q["type"] == "choice" and list(q["criteria"]) == TARGET_OPTIONS for q in mapping["questions"].values())
    assert all("options" not in q for q in mapping["questions"].values())

    checked = {q["instructions"]["column"]["header"] for q in follow_up["questions"].values()}
    assert all(q["type"] == "noul" for q in follow_up["questions"].values())
    # price/measurement columns are double-checked; confident sku/name/currency columns are not
    assert checked == {"Unit Cost", "Extended Amount", "Rated Load", "Working Reach"}


def test_no_follow_up_call_when_nothing_needs_checking(monkeypatch):
    requests = []
    result = map_columns(monkeypatch, headers=["Part Number", "Product Description"], requests=requests)
    assert len(requests) == 1
    assert result.column_mappings == {"Part Number": "sku", "Product Description": "name"}


# --- Per-field decisions ---------------------------------------------------------------------------

def test_competing_price_columns_are_resolved_by_the_checks(monkeypatch):
    result = map_columns(monkeypatch)

    assert result.is_live is True
    assert result.execution_mode.startswith("LIVE")
    assert result.model_name == "jev-1.14.0"
    assert result.column_mappings["Unit Cost"] == "unit_price"
    assert result.column_mappings["Extended Amount"] == "irrelevant"
    assert result.field_decisions["Extended Amount"]["verdict"] == "ruled_out"
    assert result.field_status["unit_price"]["status"] == "mapped"
    assert result.field_status["unit_price"]["column"] == "Unit Cost"
    assert result.mapping_issues == []
    assert result.unresolved_required_fields == []


def test_low_confidence_choice_is_rescued_by_a_confident_check(monkeypatch):
    result = map_columns(monkeypatch, choices={"Rated Load": ("payload_kg", 0.6)}, checks={("Rated Load", "payload_kg"): 0.9})
    assert result.field_decisions["Rated Load"]["verdict"] == "confirmed"
    assert result.column_mappings["Rated Load"] == "payload_kg"


def test_uncertain_optional_column_is_not_forced(monkeypatch):
    result = map_columns(monkeypatch, choices={"Rated Load": ("payload_kg", 0.6)}, checks={("Rated Load", "payload_kg"): 0.5})
    assert result.column_mappings["Rated Load"] == "unmapped"
    assert result.field_status["payload_kg"]["status"] == "uncertain"
    assert any("No confident column for 'payload_kg'" in issue for issue in result.mapping_issues)
    assert result.unresolved_required_fields == []


def test_uncertain_required_column_is_reported(monkeypatch):
    result = map_columns(monkeypatch, choices={"Product Description": ("name", 0.55)}, checks={("Product Description", "name"): 0.5})
    assert result.column_mappings["Product Description"] == "unmapped"
    assert result.unresolved_required_fields == ["name"]
    assert any("No confident column for 'name'" in issue for issue in result.mapping_issues)


def test_two_confirmed_columns_for_one_field_are_a_conflict(monkeypatch):
    result = map_columns(monkeypatch, headers=HEADERS + ["Item Label"])
    assert result.field_status["name"]["status"] == "conflict"
    assert result.column_mappings["Product Description"] == "unmapped"
    assert result.column_mappings["Item Label"] == "unmapped"
    assert result.unresolved_required_fields == ["name"]
    assert any("Competing columns for 'name'" in issue for issue in result.mapping_issues)


def test_check_can_rule_out_a_confident_choice(monkeypatch):
    result = map_columns(monkeypatch, checks={("Working Reach", "reach_m"): 0.1})
    assert result.column_mappings["Working Reach"] == "irrelevant"
    assert result.field_status["reach_m"]["status"] == "absent"
    assert result.mapping_issues == []  # reach is optional: absent is not an issue


def test_missing_required_column_is_reported(monkeypatch):
    headers = [h for h in HEADERS if h != "Product Description"]
    result = map_columns(monkeypatch, headers=headers)
    assert result.unresolved_required_fields == ["name"]
    assert "No column in the file was identified as 'name'" in result.mapping_issues


def test_missing_answer_is_never_trusted(monkeypatch):
    def handler(request):
        response = fake_jev()(request)
        body = response.json()
        body["answers"].pop("map_col_0", None)  # no answer for "Part Number"
        return httpx.Response(200, json=body)

    result = make_service(monkeypatch, handler).map_columns(HEADERS, {h: [] for h in HEADERS})
    assert result.field_decisions["Part Number"]["verdict"] == "unanswered"
    assert result.column_mappings["Part Number"] == "unmapped"
    assert "sku" in result.unresolved_required_fields


# --- Evidence, retries and failure handling --------------------------------------------------------

def test_every_live_call_is_saved_with_the_key_redacted(monkeypatch):
    result = map_columns(monkeypatch)

    assert [call["round"] for call in result.calls] == ["mapping", "follow_up"]
    assert len(result.evidence_files) == 2
    for path in result.evidence_files:
        text = Path(path).read_text(encoding="utf-8")
        record = json.loads(text)
        assert API_KEY not in text
        assert record["outcome"] == "success"
        assert record["request"]["headers"]["Authorization"] == "Bearer ***REDACTED***"


def test_transient_overload_is_retried(monkeypatch):
    attempts = []
    answer = fake_jev()

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(529, text="overloaded")
        return answer(request)

    result = make_service(monkeypatch, handler).map_columns(HEADERS, {h: [] for h in HEADERS})
    assert len(attempts) == 3  # 529 + retry for the mapping round, then the follow-up round
    assert result.is_live is True


def test_auth_error_falls_back_to_a_labelled_mock_in_auto_mode(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401, json={"error": "invalid api key"})

    result = make_service(monkeypatch, handler).map_columns(HEADERS, {h: [] for h in HEADERS})
    assert len(attempts) == 1  # 401 is not retried
    assert result.is_live is False
    assert result.execution_mode.startswith("MOCK")
    assert "mock" in result.model_name
    assert "HTTP 401" in result.blocker_message
    assert result.calls[0]["outcome"] == "failed"


def test_failed_follow_up_never_mixes_live_and_mock_answers(monkeypatch):
    answer = fake_jev()
    rounds = []

    def handler(request):
        rounds.append(request)
        if len(rounds) == 1:
            return answer(request)
        return httpx.Response(422, json={"detail": "bad question"})

    result = make_service(monkeypatch, handler).map_columns(HEADERS, {h: [] for h in HEADERS})
    assert result.is_live is False
    assert "mock" in result.model_name
    assert [(c["round"], c["outcome"]) for c in result.calls] == [("mapping", "success"), ("follow_up", "failed")]


def test_live_mode_raises_instead_of_using_the_mock(monkeypatch):
    service = make_service(monkeypatch, lambda request: httpx.Response(422, json={"detail": "bad"}), mode="live")
    with pytest.raises(JevApiError) as error:
        service.map_columns(HEADERS, {h: [] for h in HEADERS})
    assert error.value.status_code == 422


def test_mock_mode_never_calls_the_network(monkeypatch):
    def handler(request):
        raise AssertionError("mock mode must not make HTTP calls")

    result = make_service(monkeypatch, handler, mode="mock").map_columns(HEADERS, {h: [] for h in HEADERS})
    assert result.execution_mode == "MOCK (JEV_MODE=mock)"
    assert result.calls == []
    assert result.column_mappings["Unit Cost"] == "unit_price"
    assert result.column_mappings["Extended Amount"] == "irrelevant"


# --- The mock handles other suppliers' headers -----------------------------------------------------

@pytest.mark.parametrize("headers, expected", [
    (
        ["SKU", "Item Label", "Price", "Max Load", "Arm Length", "Curr.", "Warehouse Location"],
        ["sku", "name", "unit_price", "payload_kg", "reach_m", "currency", "irrelevant"],
    ),
    (
        ["Product Code", "Product Name", "Cost", "Total", "Reach (mm)", "Currency Code"],
        ["sku", "name", "unit_price", "irrelevant", "reach_m", "currency"],
    ),
])
def test_mock_maps_other_supplier_headers(headers, expected):
    result = JevService().map_columns(headers, {h: [] for h in headers})
    assert [result.column_mappings[h] for h in headers] == expected
    assert result.mapping_issues == []


def test_mock_leaves_an_ambiguous_column_unmapped():
    headers = ["SKU", "Item Label", "Price", "Weight"]
    result = JevService().map_columns(headers, {h: [] for h in headers})
    assert result.column_mappings["Weight"] == "unmapped"
    assert result.field_status["payload_kg"]["status"] == "uncertain"
