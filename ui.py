"""Gradio UI for Provenance Guard.

Run with: python ui.py
Launches on http://localhost:7860
Shares provenance.db with the Flask API but bypasses rate limiting.
"""
import base64
import os

from dotenv import load_dotenv

load_dotenv()

import gradio as gr

from audit import (
    file_appeal,
    get_analytics,
    get_certificate,
    get_log,
    get_submission,
    init_db,
    issue_certificate,
)
from pipeline import process_text_submission
from signals import classify, generate_label, get_image_llm_score

init_db()


# ---------------------------------------------------------------------------
# Tab 1 — Analyze Text
# ---------------------------------------------------------------------------

def run_analyze_text(text: str, creator_id: str):
    if not text or not text.strip():
        return "Error: text is required.", {}
    if not creator_id or not creator_id.strip():
        return "Error: creator_id is required.", {}
    try:
        result = process_text_submission(text.strip(), creator_id.strip())
    except Exception as e:
        return f"Error: Classification service unavailable — {e}", {}
    return result["label"], {
        "content_id": result["content_id"],
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "signal_1_score": result["signal_1_score"],
        "signal_2_score": result["signal_2_score"],
        "signal_3_score": result["signal_3_score"],
    }


# ---------------------------------------------------------------------------
# Tab 2 — Analyze Image
# ---------------------------------------------------------------------------

def _detect_media_type(image_bytes: bytes, path: str) -> str:
    """Detect image media type from magic bytes, falling back to extension."""
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    if image_bytes[:2] == b'BM':
        return "image/bmp"
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    return f"image/{ext}" if ext else "image/jpeg"


def run_analyze_image(image_path: str, creator_id: str):
    if not image_path:
        return "Error: please upload an image.", {}
    if not creator_id or not creator_id.strip():
        return "Error: creator_id is required.", {}

    import uuid

    from audit import write_submission

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    image_b64 = base64.b64encode(image_bytes).decode()
    media_type = _detect_media_type(image_bytes, image_path)

    content_id = str(uuid.uuid4())
    try:
        signal_1_score = get_image_llm_score(image_b64, media_type)
    except Exception as e:
        return f"Error: Classification service unavailable — {e}", {}

    notes = None
    if signal_1_score is None:
        notes = "image_parse_error"
        attribution = "uncertain"
        confidence = 0.5
    else:
        attribution, confidence = classify(signal_1_score)

    label = generate_label(attribution, confidence)
    write_submission(
        content_id=content_id,
        creator_id=creator_id.strip(),
        attribution=attribution,
        confidence=confidence,
        signal_1_score=signal_1_score,
        signal_2_score=None,
        signal_3_score=None,
        label=label,
        notes=notes,
        content_type="image",
    )

    full_label = label + "\n\nNote: Image analysis uses Signal 1 only. Signals 2 and 3 are text-only."
    return full_label, {
        "content_id": content_id,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "signal_1_score": signal_1_score,
    }


# ---------------------------------------------------------------------------
# Tab 3 — File Appeal
# ---------------------------------------------------------------------------

def run_file_appeal(content_id: str, creator_reasoning: str):
    if not content_id or not content_id.strip():
        return "Error: content_id is required."
    if not creator_reasoning or not creator_reasoning.strip():
        return "Error: creator_reasoning is required."

    submission = get_submission(content_id.strip())
    if submission is None:
        return f"Error: No submission found with content_id '{content_id.strip()}'."
    if submission["status"] == "under_review":
        return "This content already has an appeal under review."

    file_appeal(content_id.strip(), creator_reasoning.strip())
    return f"Appeal received. Status updated to under_review for content_id: {content_id.strip()}"


# ---------------------------------------------------------------------------
# Tab 4 — Verify Human (Certificate)
# ---------------------------------------------------------------------------

def run_verify(content_id: str, verification_statement: str):
    if not content_id or not content_id.strip():
        return "Error: content_id is required.", {}
    if not verification_statement or not verification_statement.strip():
        return "Error: verification_statement is required.", {}

    submission = get_submission(content_id.strip())
    if submission is None:
        return f"Error: No submission found with content_id '{content_id.strip()}'.", {}

    if submission.get("certificate_id"):
        cert = get_certificate(submission["certificate_id"])
        return "Certificate already issued.", cert

    if submission["attribution"] == "likely_ai" and submission["status"] == "classified":
        return (
            "Error: Submit an appeal before requesting verification. "
            "The appeal is your formal declaration of human authorship."
        ), {}

    cert_id = issue_certificate(
        content_id.strip(), submission["creator_id"], verification_statement.strip()
    )
    cert = get_certificate(cert_id)
    return "Verified Human credential issued.", cert


# ---------------------------------------------------------------------------
# Tab 5 — Analytics
# ---------------------------------------------------------------------------

def run_analytics():
    data = get_analytics()
    dist = data.get("attribution_distribution", {})
    summary = (
        f"Total submissions: {data['total_submissions']}\n"
        f"Appeal rate: {data['appeal_rate']:.1%}\n"
        f"Signal agreement rate: {data['signal_agreement_rate'] if data['signal_agreement_rate'] is not None else 'N/A'}"
    )
    return summary, dist


# ---------------------------------------------------------------------------
# Tab 6 — View Log
# ---------------------------------------------------------------------------

def run_log(limit: int):
    import pandas as pd
    entries = get_log(int(limit))
    if not entries:
        return pd.DataFrame()
    cols = [
        "content_id", "creator_id", "timestamp", "attribution",
        "confidence", "signal_1_score", "signal_2_score", "signal_3_score",
        "status", "content_type",
    ]
    rows = []
    for e in entries:
        rows.append({c: e.get(c) for c in cols})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Build interface
# ---------------------------------------------------------------------------

with gr.Blocks(title="Provenance Guard") as demo:
    gr.Markdown("# Provenance Guard\nAI content attribution for creative writing platforms.")

    with gr.Tabs():

        with gr.Tab("Analyze Text"):
            with gr.Row():
                with gr.Column():
                    t_text = gr.Textbox(label="Text to analyze", lines=6)
                    t_creator = gr.Textbox(label="Creator ID")
                    t_btn = gr.Button("Analyze", variant="primary")
                with gr.Column():
                    t_label = gr.Textbox(label="Transparency label", lines=5, interactive=False)
                    t_result = gr.JSON(label="Full result")
            t_btn.click(run_analyze_text, inputs=[t_text, t_creator], outputs=[t_label, t_result])

        with gr.Tab("Analyze Image"):
            with gr.Row():
                with gr.Column():
                    i_image = gr.Image(type="filepath", label="Upload image")
                    i_creator = gr.Textbox(label="Creator ID")
                    i_btn = gr.Button("Analyze", variant="primary")
                with gr.Column():
                    i_label = gr.Textbox(label="Transparency label", lines=5, interactive=False)
                    i_result = gr.JSON(label="Full result")
            i_btn.click(run_analyze_image, inputs=[i_image, i_creator], outputs=[i_label, i_result])

        with gr.Tab("File Appeal"):
            a_content_id = gr.Textbox(label="Content ID")
            a_reasoning = gr.Textbox(label="Your reasoning", lines=4)
            a_btn = gr.Button("Submit Appeal", variant="primary")
            a_output = gr.Textbox(label="Result", interactive=False)
            a_btn.click(run_file_appeal, inputs=[a_content_id, a_reasoning], outputs=a_output)

        with gr.Tab("Verify Human"):
            v_content_id = gr.Textbox(label="Content ID")
            v_statement = gr.Textbox(label="Verification statement", lines=4)
            v_btn = gr.Button("Request Certificate", variant="primary")
            v_message = gr.Textbox(label="Result", interactive=False)
            v_cert = gr.JSON(label="Certificate")
            v_btn.click(run_verify, inputs=[v_content_id, v_statement], outputs=[v_message, v_cert])

        with gr.Tab("Analytics"):
            an_btn = gr.Button("Refresh")
            an_summary = gr.Textbox(label="Summary", interactive=False)
            an_dist = gr.JSON(label="Attribution distribution")
            an_btn.click(run_analytics, outputs=[an_summary, an_dist])

        with gr.Tab("View Log"):
            l_limit = gr.Number(label="Limit", value=10, precision=0)
            l_btn = gr.Button("Load")
            l_table = gr.DataFrame(label="Audit log")
            l_btn.click(run_log, inputs=l_limit, outputs=l_table)


if __name__ == "__main__":
    demo.launch()
