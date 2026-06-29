"""Shared text submission pipeline — called by both Flask (/submit) and Gradio (ui.py).

Keeps the detection logic in one place so neither interface can drift from the other.
"""
import uuid

from audit import write_submission
from signals import (
    classify,
    combine_scores,
    generate_label,
    get_llm_score,
    get_phrase_density_score,
    get_stylometric_score,
)


def process_text_submission(text: str, creator_id: str) -> dict:
    """Run the full three-signal pipeline and write to the audit log.

    Returns a dict with keys: content_id, attribution, confidence, label,
    signal_1_score, signal_2_score, signal_3_score, notes.

    Raises on Groq network/API failure so the caller can return an appropriate error.
    """
    content_id = str(uuid.uuid4())

    signal_2_score = get_stylometric_score(text)
    signal_3_score = get_phrase_density_score(text)

    # Raises on network/API failure — caller handles 502
    signal_1_score = get_llm_score(text)

    notes = None
    if signal_1_score is None:
        notes = "signal_1_parse_error"
        attribution = "uncertain"
        confidence = 0.5
    else:
        raw_score = combine_scores(signal_1_score, signal_2_score, signal_3_score)
        attribution, confidence = classify(raw_score)

    label = generate_label(attribution, confidence)

    write_submission(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        signal_1_score=signal_1_score,
        signal_2_score=signal_2_score,
        signal_3_score=signal_3_score,
        label=label,
        notes=notes,
    )

    return {
        "content_id": content_id,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "label": label,
        "signal_1_score": signal_1_score,
        "signal_2_score": signal_2_score,
        "signal_3_score": signal_3_score,
        "notes": notes,
    }
