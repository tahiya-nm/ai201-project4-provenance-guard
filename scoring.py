# scoring.py
# ─────────────────────────────────────────────────────────────────────────────
# Ensemble confidence scoring and transparency label generation.
#
# Steps performed by this module:
#   compute_confidence() — Combines 3 signal scores via weighted average,
#                          maps result to attribution category
#   generate_label()     — Maps (attribution, confidence) to exact label text
#
# Ensemble weights (documented for stretch: ensemble detection):
#   LLM:         50% — semantic/holistic; most reliable signal
#   Stylometric: 30% — structural/statistical; well-established approach
#   Burstiness:  20% — local variation proxy; useful but noisier
#
# Decision thresholds (biased against false positives):
#   >= 0.70  → likely_ai      (require strong signal before labeling AI-generated)
#   >= 0.40  → uncertain      (wide band — benefit of the doubt to creators)
#   <  0.40  → likely_human
#
# Time:  O(1) for both functions
# Space: O(1)
# ─────────────────────────────────────────────────────────────────────────────

# Ensemble weights — must sum to 1.0
SIGNAL_WEIGHTS = {
    "llm":         0.50,
    "stylometric": 0.30,
    "burstiness":  0.20,
}

# Decision thresholds — skewed toward human-friendly verdicts
THRESHOLD_AI    = 0.70   # confidence >= this → likely_ai
THRESHOLD_HUMAN = 0.40   # confidence <  this → likely_human
                         # between both thresholds → uncertain


def compute_confidence(llm_score: float,
                       stylometric_score: float,
                       burstiness_score: float) -> tuple[float, str]:
    """
    Combines three signal scores into a single weighted confidence score
    and maps it to an attribution category.

    Steps:
      1. Compute weighted average of the three scores
      2. Clamp result to [0.0, 1.0]
      3. Map to attribution category using documented thresholds

    Args:
        llm_score:          Signal 1 (0.0–1.0, higher = more AI-like)
        stylometric_score:  Signal 2 (0.0–1.0, higher = more AI-like)
        burstiness_score:   Signal 3 (0.0–1.0, higher = more AI-like)

    Returns:
        (confidence: float, attribution: str)
        attribution ∈ {"likely_ai", "uncertain", "likely_human"}

    Time:  O(1)
    Space: O(1)
    """
    # Step 1: Weighted average
    confidence = (
        llm_score         * SIGNAL_WEIGHTS["llm"]         +
        stylometric_score * SIGNAL_WEIGHTS["stylometric"]  +
        burstiness_score  * SIGNAL_WEIGHTS["burstiness"]
    )

    # Step 2: Clamp to valid range
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    # Step 3: Map to attribution category
    if confidence >= THRESHOLD_AI:
        attribution = "likely_ai"
    elif confidence < THRESHOLD_HUMAN:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    return confidence, attribution


def generate_label(attribution: str, confidence: float) -> str:
    """
    Generates the transparency label text shown to platform users.

    Three variants (exact text matches planning.md spec):
      likely_ai:    ⚠️  AI warning with appeal option surfaced
      uncertain:    🔍  informational only, no penalty language
      likely_human: ✅  positive affirmation of original work

    Steps:
      1. Compute display percentages (AI% and human%)
      2. Select variant based on attribution
      3. Interpolate confidence percentage into label string

    Args:
        attribution: "likely_ai" | "uncertain" | "likely_human"
        confidence:  Ensemble score (0.0–1.0)

    Returns:
        Full label string suitable for display to end users.

    Time:  O(1)
    Space: O(1)
    """
    ai_pct    = round(confidence * 100)
    human_pct = round((1.0 - confidence) * 100)

    if attribution == "likely_ai":
        return (
            f"⚠️ Likely AI-Generated — "
            f"This content shows strong indicators of AI generation ({ai_pct}% confidence). "
            f"AI detection is not perfect. "
            f"If this is your own original writing, you may submit an appeal."
        )

    elif attribution == "likely_human":
        return (
            f"✅ Appears Human-Written — "
            f"This content shows strong indicators of human authorship ({human_pct}% confidence). "
            f"Thank you for contributing original work."
        )

    else:  # uncertain
        return (
            f"🔍 Attribution Uncertain — "
            f"Our system could not confidently determine the origin of this content "
            f"({ai_pct}% AI-leaning confidence). "
            f"No action has been taken. This label is for informational purposes only."
        )