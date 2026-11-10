"""
Live JEV connectivity check. Runs the real two-round column mapping on three sample columns with
JEV_MODE forced to "live" (no mock fallback), then reports what happened. Never prints the API key.

Run from the project root:  python -m scripts.check_jev
Exit code 0 = live calls succeeded, 1 = they did not (the reason is printed).
"""
import sys

from app.core.config import settings
from app.services.jev import JevApiError, JevService

SAMPLE_HEADERS = ["Part Number", "Unit Cost", "Extended Amount"]
SAMPLE_VALUES = {
    "Part Number": ["RB-1001", "RB-1002"],
    "Unit Cost": ["$1,250.00", "€ 2.450,50"],
    "Extended Amount": ["$6,250.00", "€ 4.901,00"],
}


def main() -> int:
    service = JevService()
    print(f"Endpoint     : {service.client.endpoint}")
    print(f"Model        : {settings.JEV_MODEL}")
    print(f"API key set  : {'yes' if settings.JEV_API_KEY.strip() else 'NO'}")

    settings.JEV_MODE = "live"  # this check must never fall back to the mock
    try:
        result = service.map_columns(SAMPLE_HEADERS, SAMPLE_VALUES)
    except JevApiError as exc:
        print(f"\nFAIL: {exc}")
        return 1

    print(f"\nLIVE OK      : {len(result.calls)} call(s), model {result.model_name}")
    for call in result.calls:
        print(f"  {call['round']:9} HTTP ok in {call['duration_ms']} ms  request_id={call['request_id']}  usage={call['usage']}")
        print(f"            evidence: {call['evidence_file']}")
    print()
    for header, decision in result.field_decisions.items():
        check = f"  check={decision['noul']:.2f}" if decision["noul"] is not None else ""
        print(f"  {header:16} choice={decision['choice']!r:13} confidence={decision['confidence']:.2f}{check}"
              f"  -> {decision['verdict']} -> {decision['target_field']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
