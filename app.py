import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from audit import file_appeal, get_log, get_submission, init_db, write_submission
from signals import classify, combine_scores, generate_label, get_llm_score, get_stylometric_score

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

    content_id = str(uuid.uuid4())

    # Signal 2 first — pure Python, always succeeds, always available to log
    signal_2_score = get_stylometric_score(text)

    try:
        signal_1_score = get_llm_score(text)
    except Exception as e:
        app.logger.error(f"[{content_id}] Groq API failure: {e}")
        return jsonify({"error": "Classification service unavailable. Please try again."}), 502

    notes = None
    if signal_1_score is None:
        # Signal 1 parse failure: classifying on signal_2 alone would violate the
        # multi-signal principle (signal_1 carries 60% weight), so default to uncertain.
        # signal_2_score is still logged as valid data.
        app.logger.warning(f"[{content_id}] Signal 1 parse failure — defaulting to uncertain")
        notes = "signal_1_parse_error"
        attribution = "uncertain"
        confidence = 0.5
    else:
        raw_score = combine_scores(signal_1_score, signal_2_score)
        attribution, confidence = classify(raw_score)

    label = generate_label(attribution, confidence)

    write_submission(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        signal_1_score=signal_1_score,
        signal_2_score=signal_2_score,
        label=label,
        notes=notes,
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "confidence": round(confidence, 4),
            "label": label,
        }
    )


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


if __name__ == "__main__":
    app.run(debug=True)
