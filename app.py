# app.py
# ─────────────────────────────────────────────────────────────────────────────
# Provenance Guard — Flask Application
#
# Boot sequence:
#   1. Load .env (GROQ_API_KEY)
#   2. init_db() — create SQLite tables if they don't exist
#   3. Configure Flask-Limiter (10/min, 100/day on /submit)
#   4. Register all routes
#
# Endpoints:
#   POST /submit              — Classify content (rate-limited)
#   POST /appeal              — Contest a classification
#   POST /verify/<content_id> — Provenance certificate (stretch)
#   GET  /log                 — Audit log viewer
#   GET  /dashboard           — Analytics dashboard (stretch)
#
# NOTE (Milestone 3): Signals 2 and 3 are placeholders (0.5) until Milestone 4.
# ─────────────────────────────────────────────────────────────────────────────

import uuid
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from signals import get_llm_score, get_stylometric_score, get_burstiness_score
from scoring import compute_confidence, generate_label
from database import (
    init_db, insert_submission, get_submission,
    update_status, insert_appeal, insert_certificate,
    get_log, get_analytics
)

load_dotenv()
app = Flask(__name__)

# ─── Rate Limiting ─────────────────────────────────────────────────────────────
# 10/minute: stops automated flooding (a legitimate creator submits infrequently)
# 100/day:   volume cap that allows active use but prevents bulk classification runs
# storage_uri="memory://" is required for Flask-Limiter >= 3.x
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# ─── Database Init ─────────────────────────────────────────────────────────────
# Creates tables on first run; safe to call on every startup (IF NOT EXISTS)
init_db()


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    """
    POST /submit — Classify a piece of content.

    Request body (JSON):
        text         (str, required)  — content to classify
        creator_id   (str, required)  — identifies the submitting creator
        content_type (str, optional)  — "text" (default) | "image_description" (stretch)

    Steps:
      1. Validate input fields and content_type
      2. Run Signal 1 (LLM); use placeholder 0.5 for Signals 2 & 3 (Milestone 3)
      3. Compute ensemble confidence + attribution category
      4. Generate transparency label
      5. Persist full result to audit log
      6. Return structured JSON response with content_id

    Time:  O(network) — dominated by Groq API call
    Space: O(len(text))
    """
    data = request.get_json()

    # Step 1: Validate required fields
    if not data or "text" not in data or "creator_id" not in data:
        return jsonify({"error": "Missing required fields: text, creator_id"}), 400

    text         = data["text"].strip()
    creator_id   = data["creator_id"]
    content_type = data.get("content_type", "text")

    if len(text) < 20:
        return jsonify({"error": "Text too short (minimum 20 characters)"}), 400

    if content_type not in ("text", "image_description"):
        return jsonify({"error": "content_type must be 'text' or 'image_description'"}), 400

    # Step 2: Run detection signals
    # Signal 1: LLM — semantic holistic assessment
    llm_score, llm_reasoning = get_llm_score(text, content_type)

    # Signal 2: Stylometric heuristics (global statistical properties)
    stylometric_score, stylometric_metrics = get_stylometric_score(text)

    # Signal 3: Burstiness analysis (local sequential variation — stretch: ensemble)
    burstiness_score, burstiness_metrics = get_burstiness_score(text)

    # Step 3: Compute ensemble confidence + attribution
    confidence, attribution = compute_confidence(
        llm_score, stylometric_score, burstiness_score
    )

    # Step 4: Generate transparency label
    label_text = generate_label(attribution, confidence)

    # Step 5: Persist to audit log
    content_id = str(uuid.uuid4())
    timestamp  = insert_submission(
        content_id        = content_id,
        creator_id        = creator_id,
        text              = text,
        attribution       = attribution,
        confidence        = confidence,
        llm_score         = llm_score,
        stylometric_score = stylometric_score,
        burstiness_score  = burstiness_score,
        label_text        = label_text,
        content_type      = content_type,
    )

    # Step 6: Return structured response
    return jsonify({
        "content_id":  content_id,
        "creator_id":  creator_id,
        "attribution": attribution,
        "confidence":  confidence,
        "label":       label_text,
        "signals": {
            "llm_score":           llm_score,
            "llm_reasoning":       llm_reasoning,
            "stylometric_score":   stylometric_score,
            "stylometric_metrics": stylometric_metrics,
            "burstiness_score":    burstiness_score,
            "burstiness_metrics":  burstiness_metrics,
        },
        "timestamp": timestamp,
        "status":    "classified",
    }), 201


@app.route("/appeal", methods=["POST"])
def appeal():
    """
    POST /appeal — Contest a content classification.

    Request body (JSON):
        content_id        (str, required) — from the original /submit response
        creator_reasoning (str, required) — explanation for the appeal (min 10 chars)

    Steps:
      1. Validate input
      2. Look up submission — 404 if not found
      3. Check not already under_review — 409 if duplicate
      4. Insert appeal record + update submission status to "under_review"
      5. Return confirmation with appeal_id

    Time:  O(1) — two DB ops
    Space: O(1)
    """
    data = request.get_json()

    if not data or "content_id" not in data or "creator_reasoning" not in data:
        return jsonify({"error": "Missing required fields: content_id, creator_reasoning"}), 400

    content_id        = data["content_id"]
    creator_reasoning = data["creator_reasoning"].strip()

    if len(creator_reasoning) < 10:
        return jsonify({"error": "creator_reasoning must be at least 10 characters"}), 400

    # Step 2: Look up the original submission
    submission = get_submission(content_id)
    if not submission:
        return jsonify({"error": f"No submission found with content_id: {content_id}"}), 404

    # Step 3: Prevent duplicate appeals
    if submission["status"] == "under_review":
        return jsonify({"error": "An appeal is already under review for this content."}), 409

    # Step 4: Log appeal + update status
    appeal_id = str(uuid.uuid4())
    timestamp = insert_appeal(appeal_id, content_id, creator_reasoning)
    update_status(content_id, "under_review")

    return jsonify({
        "appeal_id":  appeal_id,
        "content_id": content_id,
        "status":     "under_review",
        "message":    "Your appeal has been received and flagged for review. No action has been taken against your content.",
        "timestamp":  timestamp,
    }), 201


@app.route("/verify/<content_id>", methods=["POST"])
def verify(content_id):
    """
    POST /verify/<content_id> — Issue a provenance certificate.
    STRETCH FEATURE: Provenance Certificate.

    Eligibility:
      - Attribution must be "likely_human" OR status must be "under_review"
      - No certificate already issued for this content

    Steps:
      1. Look up submission — 404 if not found
      2. Check eligibility — 403 if likely_ai and not appealed
      3. Check no duplicate certificate — 409 if already issued
      4. Insert certificate + update status to "verified"
      5. Return certificate details including badge_display text

    Time:  O(1)
    Space: O(1)
    """
    submission = get_submission(content_id)
    if not submission:
        return jsonify({"error": f"No submission found with content_id: {content_id}"}), 404

    # Step 2: Eligibility — block unappealed AI-classified content
    is_ai           = submission["attribution"] == "likely_ai"
    is_under_review = submission["status"]      == "under_review"
    if is_ai and not is_under_review:
        return jsonify({
            "error": (
                "Provenance certificate cannot be issued for content classified as "
                "likely AI-generated. Submit an appeal first if you believe this is incorrect."
            )
        }), 403

    # Step 3: No duplicate certificates
    if submission.get("certificate_id"):
        return jsonify({
            "error":                   "A certificate has already been issued for this content.",
            "existing_certificate_id": submission["certificate_id"],
        }), 409

    # Step 4: Issue certificate
    certificate_id = "CERT-" + str(uuid.uuid4())[:8].upper()
    issued_at      = insert_certificate(certificate_id, content_id, submission["creator_id"])
    update_status(content_id, "verified")

    return jsonify({
        "certificate_id": certificate_id,
        "content_id":     content_id,
        "creator_id":     submission["creator_id"],
        "issued_at":      issued_at,
        "badge_display":  f"✅ Verified Human | Certificate #{certificate_id} | Issued {issued_at[:10]}",
        "status":         "verified",
        "message":        "Provenance certificate issued. This content is marked as verified human-written.",
    }), 201


@app.route("/log", methods=["GET"])
def log():
    """
    GET /log — Returns structured audit log entries, newest first.
    Optional query param: ?limit=N (default 50, max reasonable = 100)
    """
    limit = request.args.get("limit", 50, type=int)
    entries = get_log(limit)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """
    GET /dashboard — Analytics dashboard (stretch feature).
    Returns total submissions, by-attribution breakdown, appeal rate,
    certificate count, and 7-day submission trend.
    """
    return jsonify(get_analytics())


@app.errorhandler(429)
def rate_limit_exceeded(e):
    """Custom 429 response — returned when rate limit is exceeded on /submit."""
    return jsonify({
        "error":   "Rate limit exceeded",
        "message": "Too many submissions. Limit: 10 per minute, 100 per day.",
        "hint":    "Wait 60 seconds before retrying.",
    }), 429


if __name__ == "__main__":
    app.run(debug=True)