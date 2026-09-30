"""How the fan-out checks are said: each statement once.

A query checked under several measures, or for a filter and a grouping on
the same calendar, finds the same thing more than once. Measured: one
refusal repeated the statement about a filter twice and the condition for
the plain rows three times.
"""

from typing import Any, Dict, List


def fan_out_notes(spreads: List[Dict[str, Any]]) -> List[str]:
    """Each statement once, then each calendar's plain rows once."""
    notes = list(dict.fromkeys(spread["note"] for spread in spreads))
    layers = list(dict.fromkeys(text for spread in spreads
                                for text in spread.get("_layers") or []))
    return notes + layers


def fan_out_public(spreads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The checks as the reply carries them: no text, no repeats."""
    shown: List[Dict[str, Any]] = []
    for spread in spreads:
        entry = {key: value for key, value in spread.items()
                 if key != "note" and not key.startswith("_")}
        if entry not in shown:
            shown.append(entry)
    return shown
