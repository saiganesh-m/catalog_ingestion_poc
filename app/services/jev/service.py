"""
Column mapping with JEV (TypeSafe AI System One), in two rounds of typed questions:

  1. `choice` - for every column: which target field does it hold, or is it irrelevant?
  2. `noul`   - a yes/no check, asked only where it matters:
                * columns mapped to a price or measurement field (a wrong mapping there silently
                  corrupts numbers, e.g. a line total read as a unit price),
                * columns below the confidence threshold,
                * columns competing for the same field.

Python then settles each target field: exactly one confirmed or confident column -> mapped.
Otherwise the field is uncertain, conflicting or absent, and nothing is forced. JEV never reads or
changes row values; it only decides which column means what.

JEV_MODE controls where answers come from:
  auto  - live JEV when a key is set; on any failure, a clearly labelled mock with the blocker reported
  live  - live JEV only; any failure raises JevApiError
  mock  - never calls the network (tests, offline demos)
Live and mock answers are never mixed within one result.
"""
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.config import settings
from .client import JevApiError, JevClient
from .evidence import save_jev_exchange
from .mock import MockJevEngine

DEFAULT_CATALOG_CONTEXT = "Supplier product catalog for industrial robotics and automation equipment"

IRRELEVANT = "irrelevant"
UNMAPPED = "unmapped"

# Target field -> description sent to JEV as the `criteria` of every column-mapping question
TARGET_FIELD_CRITERIA: Dict[str, str] = {
    "sku": "Unique product identifier, e.g. part number, product code, SKU or item code",
    "name": "Human-readable product name, title or description",
    "unit_price": "Price of ONE single unit (not an extended, line or order total)",
    "currency": "Currency code or currency name that the prices are quoted in",
    "payload_kg": "Payload or rated load: the mass the product can carry or lift",
    "reach_m": "Reach: the maximum working distance or arm length",
    IRRELEVANT: "None of the above, e.g. warranty, stock location, lead time, quantity or totals",
}
TARGET_OPTIONS = list(TARGET_FIELD_CRITERIA)
TARGET_FIELDS = [name for name in TARGET_OPTIONS if name != IRRELEVANT]

# Fields that must come from a mapped column (currency can also come from a symbol in the price)
REQUIRED_FIELDS = ("sku", "name", "unit_price")
# Fields where a confident-looking mapping is still double-checked with a yes/no question
CONFIRM_FIELDS = ("unit_price", "payload_kg", "reach_m")

NOUL_YES = 0.70  # yes/no answer at or above this confirms the mapping
NOUL_NO = 0.30   # at or below this rules it out
SAMPLES_PER_COLUMN = 3

# ask(payload, round_name) -> System One response body
AskFn = Callable[[Dict[str, Any], str], Dict[str, Any]]


@dataclass
class JevMappingResult:
    """Complete decision output from JEV (or the labelled mock) for one worksheet."""
    column_mappings: Dict[str, str]             # header -> target field, "irrelevant" or "unmapped"
    field_status: Dict[str, Dict[str, Any]]     # target field -> how it was resolved (mapped/uncertain/conflict/absent)
    field_decisions: Dict[str, Dict[str, Any]]  # header -> JEV answers, verdict and note
    mapping_issues: List[str]                   # review reasons that apply to every row of the sheet
    unresolved_required_fields: List[str]       # required fields with no trustworthy column
    overall_confidence: float                   # mean choice confidence (information only)
    execution_mode: str = ""
    model_name: str = ""
    raw_api_responses: List[Dict[str, Any]] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)  # live calls: round, outcome, request_id, evidence_file
    is_live: bool = False
    blocker_message: Optional[str] = None

    @property
    def request_ids(self) -> List[str]:
        return [call["request_id"] for call in self.calls if call.get("request_id")]

    @property
    def evidence_files(self) -> List[str]:
        return [call["evidence_file"] for call in self.calls if call.get("evidence_file")]


class JevService:
    """Maps supplier spreadsheet columns to the target schema using JEV typed questions."""

    def __init__(self, client: Optional[JevClient] = None, mock: Optional[MockJevEngine] = None):
        self.client = client or JevClient()
        self.mock = mock or MockJevEngine()

    # ------------------------------------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------------------------------------

    def map_columns(
        self,
        headers: List[str],
        column_samples: Dict[str, List[str]],
        catalog_context: str = DEFAULT_CATALOG_CONTEXT,
    ) -> JevMappingResult:
        """Ask JEV (or the labelled mock, per JEV_MODE) which target field each column holds."""
        mode = settings.JEV_MODE

        if mode == "mock":
            return self._resolve(headers, column_samples, catalog_context, self._ask_mock, "MOCK", "MOCK (JEV_MODE=mock)")

        if not settings.JEV_API_KEY.strip():
            blocker = "JEV_API_KEY is not configured in .env"
            if mode == "live":
                raise JevApiError(blocker)
            result = self._resolve(headers, column_samples, catalog_context, self._ask_mock, "MOCK", "MOCK (fallback: no API key)")
            result.blocker_message = blocker
            return result

        calls: List[Dict[str, Any]] = []
        try:
            result = self._resolve(
                headers, column_samples, catalog_context,
                lambda payload, round_name: self._ask_live(payload, round_name, calls),
                "JEV", "LIVE (TypeSafe JEV API)",
            )
        except JevApiError as exc:
            if mode == "live":
                raise
            result = self._resolve(
                headers, column_samples, catalog_context, self._ask_mock, "MOCK", "MOCK (fallback: live JEV call failed)"
            )
            result.blocker_message = f"Live JEV call failed: {exc}"
            result.calls = calls  # the failed live attempt stays in the audit trail
            return result

        result.is_live = True
        result.calls = calls
        return result

    # ------------------------------------------------------------------------------------------------
    # Answer sources
    # ------------------------------------------------------------------------------------------------

    def _ask_live(self, payload: Dict[str, Any], round_name: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            http = self.client.system_one(payload)
        except JevApiError as exc:
            evidence_file = save_jev_exchange(
                endpoint=self.client.endpoint, request_payload=payload, status_code=exc.status_code,
                request_id=exc.request_id, response_body=exc.body, error=str(exc),
            )
            calls.append({
                "round": round_name, "outcome": "failed", "status_code": exc.status_code,
                "request_id": exc.request_id, "evidence_file": evidence_file,
            })
            raise

        evidence_file = save_jev_exchange(
            endpoint=self.client.endpoint, request_payload=payload, status_code=http.status_code,
            request_id=http.request_id, response_body=http.response, duration_ms=http.duration_ms,
            attempts=http.attempts,
        )
        calls.append({
            "round": round_name, "outcome": "success", "request_id": http.request_id,
            "duration_ms": http.duration_ms, "usage": http.response.get("usage"), "evidence_file": evidence_file,
        })
        return http.response

    def _ask_mock(self, payload: Dict[str, Any], round_name: str) -> Dict[str, Any]:
        return self.mock.respond(payload)

    # ------------------------------------------------------------------------------------------------
    # The two rounds and the decision
    # ------------------------------------------------------------------------------------------------

    def _resolve(
        self,
        headers: List[str],
        column_samples: Dict[str, List[str]],
        catalog_context: str,
        ask: AskFn,
        source: str,
        execution_mode: str,
    ) -> JevMappingResult:
        columns = {
            header: {"header": header, "sample_values": column_samples.get(header, [])[:SAMPLES_PER_COLUMN]}
            for header in headers
        }
        state = {"catalog_context": catalog_context, "columns": list(columns.values())}

        # Round 1: which field does each column hold?
        choice_ids = {f"map_col_{index}": header for index, header in enumerate(headers)}
        mapping_response = ask(
            self._payload(state, {qid: self._choice_question(columns[header]) for qid, header in choice_ids.items()}),
            "mapping",
        )
        decisions = {header: self._read_choice(mapping_response, qid, header) for qid, header in choice_ids.items()}
        responses = [mapping_response]

        # Round 2: yes/no checks where a wrong or uncertain mapping would matter
        check_ids = self._follow_up_candidates(headers, decisions)
        if check_ids:
            follow_up_response = ask(
                self._payload(state, {
                    qid: self._check_question(columns[header], decisions[header]["choice"])
                    for qid, header in check_ids.items()
                }),
                "follow_up",
            )
            responses.append(follow_up_response)
            for qid, header in check_ids.items():
                decisions[header]["check_question_id"] = qid
                decisions[header]["noul"] = self._read_noul(follow_up_response, qid)

        for decision in decisions.values():
            decision["verdict"] = self._verdict(decision)
            decision["note"] = self._note(decision, source)

        column_mappings, field_status = self._settle_fields(decisions)
        mapping_issues, unresolved = self._mapping_issues(field_status)
        for header, decision in decisions.items():
            decision["target_field"] = column_mappings[header]

        confidences = [decision["confidence"] for decision in decisions.values()]
        return JevMappingResult(
            column_mappings=column_mappings,
            field_status=field_status,
            field_decisions=decisions,
            mapping_issues=mapping_issues,
            unresolved_required_fields=unresolved,
            overall_confidence=round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
            execution_mode=execution_mode,
            model_name=str(mapping_response.get("model") or settings.JEV_MODEL),
            raw_api_responses=responses,
        )

    @staticmethod
    def _payload(state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Any]:
        return {"model": settings.JEV_MODEL, "state": state, "questions": questions}

    @staticmethod
    def _choice_question(column: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "choice",
            "instructions": {
                "column": column,
                "question": "Which standard product field does the supplier spreadsheet column `column` hold?",
            },
            "criteria": TARGET_FIELD_CRITERIA,
        }

    @staticmethod
    def _check_question(column: Dict[str, Any], candidate_field: str) -> Dict[str, Any]:
        meaning = TARGET_FIELD_CRITERIA[candidate_field]
        return {
            "type": "noul",
            "instructions": {
                "column": column,
                "candidate_field": {"name": candidate_field, "meaning": meaning},
                "question": "Does `column` hold `candidate_field`?",
            },
            "criteria": {"true": f"Yes - {meaning}", "false": "No - the column holds something else"},
        }

    @staticmethod
    def _read_choice(response: Dict[str, Any], question_id: str, header: str) -> Dict[str, Any]:
        answer = (response.get("answers") or {}).get(question_id)
        valid = isinstance(answer, dict) and answer.get("type") == "choice" and answer.get("choice") in TARGET_FIELD_CRITERIA
        return {
            "column_name": header,
            "question_id": question_id,
            "choice": answer["choice"] if valid else None,
            "confidence": float(answer.get("confidence", 0.0)) if valid else 0.0,
            "probabilities": (answer.get("probabilities") or {}) if valid else {},
            "check_question_id": None,
            "noul": None,
        }

    @staticmethod
    def _read_noul(response: Dict[str, Any], question_id: str) -> Optional[float]:
        answer = (response.get("answers") or {}).get(question_id)
        if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)):
            return float(answer["noul"])
        return None

    @staticmethod
    def _follow_up_candidates(headers: List[str], decisions: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        """Columns that get a yes/no check: risky fields, low confidence, or competing for one field."""
        chosen = Counter(d["choice"] for d in decisions.values() if d["choice"] not in (None, IRRELEVANT))
        candidates: Dict[str, str] = {}
        for index, header in enumerate(headers):
            choice = decisions[header]["choice"]
            if choice in (None, IRRELEVANT):
                continue
            if (
                choice in CONFIRM_FIELDS
                or decisions[header]["confidence"] < settings.CONFIDENCE_THRESHOLD
                or chosen[choice] > 1
            ):
                candidates[f"check_col_{index}"] = header
        return candidates

    @staticmethod
    def _verdict(decision: Dict[str, Any]) -> str:
        """unanswered | irrelevant | confirmed | ruled_out | uncertain | confident"""
        if decision["choice"] is None:
            return "unanswered"
        if decision["choice"] == IRRELEVANT:
            return "irrelevant"
        if decision["check_question_id"]:
            noul = decision["noul"]
            if noul is None:
                return "uncertain"
            if noul >= NOUL_YES:
                return "confirmed"
            if noul <= NOUL_NO:
                return "ruled_out"
            return "uncertain"
        return "confident" if decision["confidence"] >= settings.CONFIDENCE_THRESHOLD else "uncertain"

    @staticmethod
    def _note(decision: Dict[str, Any], source: str) -> Optional[str]:
        verdict, choice = decision["verdict"], decision["choice"]
        confidence, noul = decision["confidence"], decision["noul"]
        if verdict == "unanswered":
            return f"{source} returned no usable answer for this column, so it was left unmapped"
        if verdict == "confirmed":
            return f"{source} check confirmed '{choice}' (yes {noul:.2f})"
        if verdict == "ruled_out":
            return f"{source} check ruled out '{choice}' (yes {noul:.2f}); treated as irrelevant"
        if verdict == "uncertain":
            check = f", check {noul:.2f}" if noul is not None else ""
            return f"Not confident enough to map to '{choice}' (choice {confidence:.2f}{check}); left unmapped"
        return None

    @staticmethod
    def _settle_fields(
        decisions: Dict[str, Dict[str, Any]],
    ) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        """Map each target field to exactly one trustworthy column, or record why it could not be."""
        column_mappings = {
            header: IRRELEVANT if decision["verdict"] in ("irrelevant", "ruled_out") else UNMAPPED
            for header, decision in decisions.items()
        }
        field_status: Dict[str, Dict[str, Any]] = {}

        for target in TARGET_FIELDS:
            candidates = [header for header, d in decisions.items() if d["choice"] == target]
            accepted = [h for h in candidates if decisions[h]["verdict"] in ("confirmed", "confident")]
            uncertain = [h for h in candidates if decisions[h]["verdict"] == "uncertain"]

            if len(accepted) == 1:
                status, column = "mapped", accepted[0]
                column_mappings[column] = target
            elif len(accepted) > 1:
                status, column = "conflict", None
                for header in accepted:
                    others = ", ".join(h for h in accepted if h != header)
                    decisions[header]["note"] = f"Competes with {others} for '{target}'; left unmapped"
            elif uncertain:
                status, column = "uncertain", None
            else:
                status, column = "absent", None

            field_status[target] = {
                "status": status,
                "column": column,
                "candidates": [
                    {
                        "column": h,
                        "confidence": decisions[h]["confidence"],
                        "noul": decisions[h]["noul"],
                        "verdict": decisions[h]["verdict"],
                    }
                    for h in candidates
                ],
                "reason": None,
            }
        return column_mappings, field_status

    @staticmethod
    def _mapping_issues(field_status: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """Turn unresolved fields into review reasons. A missing optional field is not an issue."""
        def describe(candidates: List[Dict[str, Any]]) -> str:
            return ", ".join(
                f"{c['column']} {c['confidence']:.2f}" + (f"/check {c['noul']:.2f}" if c["noul"] is not None else "")
                for c in candidates
            )

        issues: List[str] = []
        unresolved: List[str] = []
        for target, status in field_status.items():
            kind = status["status"]
            if kind == "mapped":
                continue
            if kind == "conflict":
                competing = [c for c in status["candidates"] if c["verdict"] in ("confirmed", "confident")]
                reason = f"Competing columns for '{target}' ({describe(competing)}); JEV could not pick one"
            elif kind == "uncertain":
                unsure = [c for c in status["candidates"] if c["verdict"] == "uncertain"]
                reason = f"No confident column for '{target}' (candidates: {describe(unsure)})"
            elif target in REQUIRED_FIELDS:
                reason = f"No column in the file was identified as '{target}'"
            else:
                continue  # optional field simply not supplied by this supplier
            status["reason"] = reason
            issues.append(reason)
            if target in REQUIRED_FIELDS:
                unresolved.append(target)
        return issues, unresolved
