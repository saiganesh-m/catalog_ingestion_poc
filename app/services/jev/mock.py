"""
Clearly labelled stand-in for the JEV API.

Used only in tests, when JEV_MODE=mock, or as a reported fallback when the live API is unavailable.
It accepts the same request and returns the same response shape as POST /v1/systemone, so the rest
of the pipeline handles both identically. It is a keyword lookup, not a model: its numbers must
never be presented as JEV output.
"""
import re
from typing import Any, Dict

MOCK_MODEL_NAME = "mock-jev (simulated - not a real JEV call)"


def _hints(distribution: Dict[str, float], *headers: str) -> Dict[str, Dict[str, float]]:
    return {header: dict(distribution) for header in headers}


# Normalised header -> probability over target fields
HEADER_HINTS: Dict[str, Dict[str, float]] = {
    **_hints({"sku": 0.96, "name": 0.04},
             "part number", "part no", "product code", "item code", "item number", "sku",
             "article number", "catalog number", "product id"),
    **_hints({"name": 0.95, "sku": 0.05},
             "product description", "description", "item name", "item label", "product name",
             "name", "title", "item description"),
    **_hints({"unit_price": 0.93, "irrelevant": 0.07},
             "unit cost", "unit price", "price", "list price", "net price", "cost", "price each"),
    **_hints({"irrelevant": 0.88, "unit_price": 0.12},
             "extended amount", "extended price", "line total", "total", "total price", "amount"),
    **_hints({"payload_kg": 0.94, "irrelevant": 0.06},
             "rated load", "payload", "max load", "load capacity", "payload capacity", "rated payload"),
    **_hints({"reach_m": 0.94, "irrelevant": 0.06},
             "working reach", "reach", "arm length", "max reach", "working range"),
    **_hints({"currency": 0.98, "irrelevant": 0.02},
             "currency", "currency code", "curr", "ccy"),
    **_hints({"irrelevant": 0.95, "name": 0.05},
             "order qty", "order quantity", "quantity", "qty", "warranty duration", "warranty months",
             "warranty", "lead time", "warehouse location", "stock location", "country of origin"),
    # Genuinely ambiguous: a product's own weight is not its payload
    **_hints({"payload_kg": 0.55, "irrelevant": 0.45}, "load", "weight"),
}
UNKNOWN_HEADER = {"irrelevant": 0.55, "name": 0.25, "sku": 0.20}


def normalise_header(header: str) -> str:
    """'Reach (mm)' -> 'reach', 'Curr.' -> 'curr', 'Part  No.' -> 'part no'."""
    without_units = re.sub(r"[(\[].*?[)\]]", " ", str(header).lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_units).split())


def _column_hint(question: Dict[str, Any]) -> Dict[str, float]:
    instructions = question.get("instructions")
    column = instructions.get("column", {}) if isinstance(instructions, dict) else {}
    return HEADER_HINTS.get(normalise_header(column.get("header", "")), UNKNOWN_HEADER)


class MockJevEngine:
    """Answers a System One request body with simulated, clearly labelled answers."""

    def respond(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        answers: Dict[str, Any] = {}
        for question_id, question in payload.get("questions", {}).items():
            if question.get("type") == "choice":
                answers[question_id] = self._choice_answer(question)
            elif question.get("type") == "noul":
                answers[question_id] = {"type": "noul", "noul": self._noul_answer(question)}
        return {
            "model": MOCK_MODEL_NAME,
            "answers": answers,
            "usage": {"input_tokens": None, "output_tokens": None},
        }

    @staticmethod
    def _choice_answer(question: Dict[str, Any]) -> Dict[str, Any]:
        hint = _column_hint(question)
        options = list(question.get("criteria", {}))
        raw = {option: hint.get(option, 0.0) for option in options}
        total = sum(raw.values()) or 1.0
        probabilities = {option: round(value / total, 4) for option, value in raw.items()}
        choice = max(probabilities, key=probabilities.get)
        return {
            "type": "choice",
            "choice": choice,
            "probabilities": probabilities,
            "confidence": probabilities[choice],
        }

    @staticmethod
    def _noul_answer(question: Dict[str, Any]) -> float:
        """Yes-probability that the column holds the candidate field, from the same keyword table."""
        instructions = question.get("instructions")
        candidate = instructions.get("candidate_field", {}) if isinstance(instructions, dict) else {}
        probability = _column_hint(question).get(candidate.get("name"), 0.0)
        return round(min(0.97, max(0.03, probability)), 2)
