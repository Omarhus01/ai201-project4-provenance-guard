import base64
import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from audit import (
    file_appeal, get_analytics, get_certificate, get_log,
    get_submission, init_db, issue_certificate, write_submission,
)
from pipeline import process_text_submission
from signals import (
    classify, generate_label,
    get_image_llm_score,
)

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

init_db()


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True)
    if not data or not data.get("text") or not data.get("creator_id"):
        return jsonify({"error": "text and creator_id are required"}), 400

    text = data["text"].strip()
    creator_id = data["creator_id"].strip()

    if not text:
        return jsonify({"error": "text cannot be empty"}), 400

    try:
        result = process_text_submission(text, creator_id)
    except Exception as e:
        app.logger.error(f"Groq API failure: {e}")
        return jsonify({"error": "Classification service unavailable. Please try again."}), 502

    if result.get("notes") == "signal_1_parse_error":
        app.logger.warning(f"[{result['content_id']}] Signal 1 parse failure — defaulting to uncertain")

    return jsonify({
        "content_id": result["content_id"],
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "label": result["label"],
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    # No rate limit on /appeal:
    # - Unknown content_ids hit the 404 guard immediately (no work done, no value to flood)
    # - Repeat appeals on valid content_ids are rejected by the already-under-review guard
    # Together these make flooding /appeal either pointless or self-limiting.
    data = request.get_json(silent=True)
    if not data or not data.get("content_id") or not data.get("creator_reasoning"):
        return jsonify({"error": "content_id and creator_reasoning are required"}), 400

    content_id = data["content_id"].strip()
    creator_reasoning = data["creator_reasoning"].strip()

    if not creator_reasoning:
        return jsonify({"error": "creator_reasoning cannot be empty"}), 400

    submission = get_submission(content_id)
    if submission is None:
        return jsonify({"error": f"No submission found with content_id '{content_id}'"}), 404

    if submission["status"] == "under_review":
        return jsonify({
            "content_id": content_id,
            "status": "under_review",
            "message": "This content already has an appeal under review.",
        }), 409

    file_appeal(content_id, creator_reasoning)

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal has been received and is under review.",
    })


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", 20, type=int)
    return jsonify({"entries": get_log(limit)})


@app.route("/analytics", methods=["GET"])
def analytics():
    return jsonify(get_analytics())


@app.route("/verify", methods=["POST"])
def verify():
    data = request.get_json(silent=True)
    if not data or not data.get("content_id") or not data.get("verification_statement"):
        return jsonify({"error": "content_id and verification_statement are required"}), 400

    content_id = data["content_id"].strip()
    statement = data["verification_statement"].strip()
    if not statement:
        return jsonify({"error": "verification_statement cannot be empty"}), 400

    submission = get_submission(content_id)
    if submission is None:
        return jsonify({"error": f"No submission found with content_id '{content_id}'"}), 404

    # Idempotent: if already certified, return the existing certificate
    if submission.get("certificate_id"):
        cert = get_certificate(submission["certificate_id"])
        return jsonify({
            "cert_id": cert["cert_id"],
            "content_id": content_id,
            "issued_at": cert["issued_at"],
            "message": "Certificate already issued.",
        })

    if submission["attribution"] == "likely_ai" and submission["status"] == "classified":
        return jsonify({
            "error": "Submit an appeal before requesting verification. "
                     "The appeal is your formal declaration of human authorship."
        }), 403

    cert_id = issue_certificate(content_id, submission["creator_id"], statement)
    cert = get_certificate(cert_id)

    return jsonify({
        "cert_id": cert_id,
        "content_id": content_id,
        "issued_at": cert["issued_at"],
        "message": "Verified Human credential issued.",
    })


@app.route("/certificate/<cert_id>", methods=["GET"])
def certificate(cert_id):
    cert = get_certificate(cert_id)
    if cert is None:
        return jsonify({"error": f"No certificate found with cert_id '{cert_id}'"}), 404
    return jsonify(cert)


@app.route("/submit/image", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit_image():
    data = request.get_json(silent=True)
    if not data or not data.get("image_b64") or not data.get("creator_id"):
        return jsonify({"error": "image_b64, media_type, and creator_id are required"}), 400
    if not data.get("media_type"):
        return jsonify({"error": "image_b64, media_type, and creator_id are required"}), 400

    image_b64 = data["image_b64"]
    media_type = data["media_type"].strip()
    creator_id = data["creator_id"].strip()

    # Validate base64 encoding
    try:
        base64.b64decode(image_b64, validate=True)
    except Exception:
        return jsonify({"error": "image_b64 is not valid base64"}), 400

    content_id = str(uuid.uuid4())

    try:
        signal_1_score = get_image_llm_score(image_b64, media_type)
    except Exception as e:
        app.logger.error(f"[{content_id}] Groq vision API failure: {e}")
        return jsonify({"error": "Classification service unavailable. Please try again."}), 502

    notes = None
    if signal_1_score is None:
        app.logger.warning(f"[{content_id}] Image Signal 1 parse failure — defaulting to uncertain")
        notes = "image_parse_error"
        attribution = "uncertain"
        confidence = 0.5
    else:
        # Single-signal path: stylometrics and phrase density have no image equivalent.
        # Image classification is inherently less reliable than text; results should be
        # treated as indicative, not definitive.
        attribution, confidence = classify(signal_1_score)

    label = generate_label(attribution, confidence)

    write_submission(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        signal_1_score=signal_1_score,
        signal_2_score=None,
        signal_3_score=None,
        label=label,
        notes=notes,
        content_type="image",
    )

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "label": label,
        "note": "Image analysis uses Signal 1 only. Signals 2 and 3 are text-only.",
    })


if __name__ == "__main__":
    app.run(debug=True)
