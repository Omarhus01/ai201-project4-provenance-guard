import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from audit import get_log, init_db, write_submission
from signals import classify, get_llm_score

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

    try:
        signal_1_score = get_llm_score(text)
    except Exception as e:
        app.logger.error(f"[{content_id}] Groq API failure: {e}")
        return jsonify({"error": "Classification service unavailable. Please try again."}), 502

    notes = None
    if signal_1_score is None:
        app.logger.warning(f"[{content_id}] Signal 1 parse failure — defaulting to uncertain")
        notes = "signal_1_parse_error"
        attribution = "uncertain"
        confidence = 0.5
    else:
        attribution, confidence = classify(signal_1_score)

    label = f"[MS3 placeholder] Attribution: {attribution}"

    write_submission(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        signal_1_score=signal_1_score,
        signal_2_score=None,
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


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", 20, type=int)
    return jsonify({"entries": get_log(limit)})


if __name__ == "__main__":
    app.run(debug=True)
