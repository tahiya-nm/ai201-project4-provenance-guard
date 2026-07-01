# Provenance Guard — Planning Document

> Written before implementation. Updated before any stretch features.

---

## Architecture

### Architecture Narrative

A piece of content enters via `POST /submit` with a `text` field, a `creator_id`, and an
optional `content_type`. The input is validated (minimum length, required fields). It is
then passed to three detection signals that run independently:

- **Signal 1** uses Groq's LLM (llama-3.3-70b-versatile) to assess whether the text
  semantically reads as AI-generated — a holistic, meaning-aware assessment.
- **Signal 2** uses stylometric heuristics (sentence length uniformity, type-token ratio,
  filler word density, average sentence length) to measure global statistical properties
  of the writing.
- **Signal 3** uses burstiness analysis to measure how much writing rhythm varies locally
  from one sentence to the next — a sequential variation signal independent of Signal 2.

Each signal returns a score from 0.0 (human-like) to 1.0 (AI-like). The three scores are
combined into a single weighted ensemble confidence score (LLM 50%, Stylometric 30%,
Burstiness 20%). That confidence score maps to an attribution category and a transparency
label. The full result — `content_id`, attribution, confidence, label text, and all three
signal scores — is written to a SQLite audit log and returned in the JSON response.

For appeals, a creator submits their `content_id` and reasoning. The system updates the
submission's status to `"under_review"` and logs the appeal alongside the original decision.
No automated re-classification occurs.

### ASCII Diagram

```
SUBMISSION FLOW:
  [Client] ──► POST /submit { text, creator_id, content_type }
                    │
              Input Validation
                    │
         ┌──────────┼──────────────┐
         ▼          ▼              ▼
   [Signal 1]  [Signal 2]    [Signal 3]
   LLM/Groq    Stylometrics   Burstiness
   semantic    global stats   local rhythm
   0.0–1.0     0.0–1.0        0.0–1.0
         └──────────┼──────────────┘
                    ▼
         [Ensemble Scorer]
          50% / 30% / 20%
                    │
              confidence (0.0–1.0)
              attribution category
                    │
         [Transparency Label Generator]
                    │
           [SQLite Audit Log]
                    │
         JSON Response → content_id, attribution,
         confidence, label, all signal scores, timestamp

APPEAL FLOW:
  [Client] ──► POST /appeal { content_id, creator_reasoning }
                    │
              Lookup content_id
                    │
         Update status → "under_review"
                    │
         Insert appeal row into DB
                    │
         JSON Response → appeal_id, status

CERTIFICATE FLOW (STRETCH):
  [Client] ──► POST /verify/<content_id>
                    │
         Check: not likely_ai OR already under_review
         Check: no duplicate certificate exists
                    │
         Insert certificate → CERT-XXXXXXXX
         Update status → "verified"
                    │
         JSON Response → certificate_id, badge_display
```

### API Surface

| Method | Endpoint                  | Purpose                                         |
|--------|---------------------------|-------------------------------------------------|
| POST   | `/submit`                 | Classify content (rate-limited: 10/min, 100/day)|
| POST   | `/appeal`                 | Contest a classification                        |
| POST   | `/verify/<content_id>`    | Request provenance certificate (stretch)        |
| GET    | `/log`                    | View structured audit log entries               |
| GET    | `/dashboard`              | Analytics dashboard (stretch)                   |

---

## Detection Signals

### Signal 1 — LLM Classification (Groq: llama-3.3-70b-versatile)

**What it measures:**
Semantic and stylistic coherence holistically. The model reads the text as a human would
and assesses whether it "reads like AI" — capturing things like hedged language patterns,
overly balanced structure, unnatural polish, and absence of idiosyncratic voice.

**Output format:**
A float between 0.0 and 1.0 (`ai_probability`), parsed from a JSON response. Also returns
a one-sentence `reasoning` string. Example: `{"ai_probability": 0.82, "reasoning": "..."}`

**Why it is distinct from Signals 2 and 3:**
This signal has semantic understanding — it can recognize meaning-level patterns (hedging,
transitional language, topic structure) that no statistical heuristic can capture. Signals
2 and 3 are purely mathematical and have no awareness of meaning.

**Blind spots:**
- Short texts (< 50 words) give insufficient context for reliable assessment.
- Very formal human writing (academic, legal) may be flagged as AI-like.
- Intentionally "humanized" AI output (added typos, colloquialisms) can fool it.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:**
Global statistical properties of the text as a whole:
1. **Sentence length uniformity** — AI text tends to have consistent sentence lengths (low
   standard deviation). Human text mixes short punchy sentences with longer elaborations.
2. **Type-token ratio (TTR)** — vocabulary diversity (unique words / total words). Very high
   TTR (> 0.85) is typically human; AI can produce consistent moderate TTR.
3. **Filler/informal word density** — words like "just", "honestly", "like", "basically",
   "yeah". Humans use these naturally; AI rarely does in formal contexts.
4. **Average sentence length** — AI tends toward longer, denser sentences on average.

**Output format:**
A float 0.0–1.0 (weighted combination of the 4 sub-features), plus a `metrics` dict with
raw measurements and individual sub-scores. Weights: uniformity 35%, TTR 25%, filler 20%,
avg length 20%.

**Why it is distinct from Signal 3:**
Signal 2 measures the *shape of the overall distribution* across the whole text — what the
average looks like, how spread out values are globally. Signal 3 measures *local sequential
changes* — how much writing rhythm varies from sentence to sentence. These are mathematically
independent: a text can have high global variance but low sequential variation (or vice versa).

**Blind spots:**
- Academic or formal human writing scores falsely high (long sentences, no fillers, formal
  vocabulary — all stylometric AI signals, despite being human).
- Non-native English speakers who write formally may similarly be penalized.
- TTR reliability drops on longer texts (naturally decreases with length).

---

### Signal 3 — Burstiness Analysis (pure Python — Stretch: Ensemble)

**What it measures:**
Local sequential variation patterns — how much sentence lengths change from one sentence to
the next within the text. Human writing is "bursty": it alternates dramatically between short
punchy sentences and long elaborative ones. AI writing maintains rhythmic consistency even
within paragraphs, even if it has some global variation.

Four sub-features:
1. **Coefficient of variation (CV)** — std deviation / mean of sentence lengths. Low CV
   (< 0.3) signals uniform AI-like rhythm.
2. **Outlier sentence ratio** — proportion of sentences that are very short (< 4 words) or
   very long (> 35 words). Humans mix dramatic extremes; AI avoids them.
3. **Max-to-min sentence length ratio** — how wide the overall length range is. Narrow range
   indicates AI-like uniformity.
4. **Average sequential difference** — mean of |length[i] - length[i-1]| for adjacent
   sentences. Low average diff = smooth, non-bursty rhythm = AI-like.

**Output format:**
A float 0.0–1.0 (weighted combination: CV 30%, outliers 25%, range 25%, seq diff 20%),
plus a `metrics` dict with raw measurements and sub-scores.

**Why it is distinct from Signal 2:**
Signal 2 asks "what does the distribution look like overall?" Signal 3 asks "how does each
sentence compare to its neighbors?" A long academic paper might have high global stylometric
variance (some very long paragraphs, some short ones) but low burstiness within each
paragraph (consistent rhythm throughout). These are different properties on different axes.

**Blind spots:**
- Poetry and intentionally rhythmic writing (consistent stanza structure) scores as AI-like.
- Texts shorter than 3 sentences cannot produce meaningful sequential analysis.
- Dialogue-heavy writing with many short exchanges may score as unexpectedly human.

---

## Confidence Scoring and Uncertainty Representation

### Ensemble Weighting

```
final_confidence = (llm_score × 0.50) + (stylometric_score × 0.30) + (burstiness_score × 0.20)
```

**Reasoning behind weights:**
- **LLM (50%):** The most reliable signal. It has semantic understanding that no
  statistical heuristic can replicate. It's the primary decision-maker.
- **Stylometric (30%):** Well-established in the literature on AI text detection.
  Provides structural grounding independent of any LLM behavior.
- **Burstiness (20%):** A useful additional signal but noisier, especially on short texts.
  Given a supporting rather than leading role.

### Decision Thresholds

```
confidence >= 0.70  →  likely_ai      (strong AI signal required before labeling)
0.40 <= conf < 0.70 →  uncertain      (wide band — benefit of the doubt)
confidence < 0.40   →  likely_human
```

**Why these thresholds (not 0.50):**
A false positive — labeling a human writer's work as AI-generated — is significantly more
harmful than a false negative on a creative writing platform. An incorrect AI label damages
a creator's reputation and undermines trust in the platform. Setting the `likely_ai` threshold
at 0.70 (not 0.50) means the system requires strong, consistent signal across all three
independent signals before making an accusation. The uncertain band (0.40–0.70) spans 30
percentage points, giving creators meaningful breathing room.

**What a score of 0.60 means:**
The system leans AI-leaning but cannot assert it with confidence. Attribution is `uncertain`.
The label is informational only and no penalty is applied. This is the correct outcome for
borderline cases — the creator is not penalized, and the uncertainty is communicated honestly.

**What a score of 0.95 means:**
All three independent signals strongly agree the text is AI-generated. This is a high-confidence
verdict and warrants the `likely_ai` label with an appeal option.

---

## Transparency Label Variants

All three variants are written out below as they will appear to end users.

### Variant 1 — High-Confidence AI (confidence >= 0.70)

> ⚠️ Likely AI-Generated — This content shows strong indicators of AI generation (X% confidence). AI detection is not perfect. If this is your own original writing, you may submit an appeal.

*(X% is the confidence score as a percentage, e.g., "78% confidence")*

### Variant 2 — Attribution Uncertain (0.40 <= confidence < 0.70)

> 🔍 Attribution Uncertain — Our system could not confidently determine the origin of this content (X% AI-leaning confidence). No action has been taken. This label is for informational purposes only.

*(X% is the AI-leaning confidence, e.g., "58% AI-leaning confidence")*

### Variant 3 — High-Confidence Human (confidence < 0.40)

> ✅ Appears Human-Written — This content shows strong indicators of human authorship (X% confidence). Thank you for contributing original work.

*(X% is the human confidence, i.e., 100 - AI%, e.g., "72% confidence")*

**Design note:** The uncertain variant deliberately avoids the word "penalty" and uses
"informational purposes only." The human variant affirms the creator positively rather than
just saying "not AI." The AI variant always surfaces the appeal option — the path forward
is never hidden from the creator.

---

## Appeals Workflow

**Who can submit an appeal:**
Any creator who submitted content, identified by supplying the `content_id` returned in
their original `/submit` response.

**What they provide:**
- `content_id` — links the appeal to the original classification decision
- `creator_reasoning` — free-text explanation (minimum 10 characters) of why they believe
  the classification is incorrect

**What the system does when an appeal is received:**
1. Looks up the `content_id` in the database. Returns 404 if not found.
2. Checks the current status is not already `"under_review"` (no duplicate appeals).
3. Inserts a row in the `appeals` table with `appeal_id`, `content_id`, `creator_reasoning`,
   and `timestamp`.
4. Updates the submission's `status` field from `"classified"` to `"under_review"`.
5. Returns a confirmation JSON with the `appeal_id` and new status.

**What a human reviewer sees:**
`GET /log` returns all submissions ordered newest-first. Each entry that has been appealed
shows: the original attribution, confidence score, all three signal scores, the creator's
reasoning in `appeal_reasoning`, and the `appeal_timestamp`. The reviewer can see both the
system's evidence and the creator's counter-argument side by side.

**Automated re-classification:** Not implemented. Resolution requires human judgment.

---

## Anticipated Edge Cases

### Edge Case 1 — Formal human writing misclassified as AI
A non-native English speaker submits a personal essay written in careful, formal English
with no contractions, long sentences, and no colloquial filler words. The stylometric signal
scores this high (AI-like) because it lacks the informal markers the signal uses as human
indicators. The LLM signal may also be uncertain because the prose is polished. If the
ensemble score lands in the 0.55–0.70 range, the result is `uncertain` — not `likely_ai` —
due to the threshold asymmetry. The creator sees the uncertain label with no penalty, and
can appeal if they want the record corrected.

### Edge Case 2 — Short text (under 80 words)
A haiku, a tweet-length post, or a single paragraph (2 sentences) will produce unreliable
burstiness scores (not enough sequential data) and inflated TTR values in stylometrics
(naturally high for short texts). The LLM signal still functions well on short text, but
the ensemble may be pulled in inconsistent directions by the noisy structural signals.
Mitigation: the wide uncertain band absorbs much of this noise; the system will more often
return `uncertain` than a wrong definitive verdict.

### Edge Case 3 — AI text with intentional humanization
An AI-generated piece that has been edited to add typos, contractions, colloquial phrases,
and irregular sentence lengths will score lower on stylometrics and burstiness (more
human-like). The LLM signal is the most robust to this but can still be fooled by
sophisticated prompt engineering. This is an unavoidable limitation — the system should
be understood as probabilistic, not definitive.

### Edge Case 4 — Poetry or highly structured creative writing
Poems with consistent stanza structures, refrains, or intentional rhythmic repetition will
score as AI-like on the burstiness signal (low sequential variation is the feature). A
villanelle or a poem with a strict meter pattern will appear rhythmically uniform by design.
The LLM signal should counteract this (it understands poetic form), but the ensemble score
may still land in `uncertain`. The appeals workflow is the correct resolution path.

---

## AI Tool Plan

### Milestone 3 — Submission Endpoint + Signal 1

**Spec sections to provide:** Detection Signals (Signal 1 only) + Architecture diagram

**What to ask the AI tool to generate:**
"Using this architecture diagram and signal description, generate: (1) a Flask app skeleton
with a `POST /submit` route stub that returns a hardcoded JSON response; (2) a `get_llm_score()`
function that sends text to Groq's llama-3.3-70b-versatile and returns a `(float, str)` tuple
where the float is `ai_probability` parsed from a JSON response."

**How to verify the output:**
Call `get_llm_score()` directly on 3 test strings before wiring into the endpoint. Confirm
the output is a plain float between 0 and 1, not a dict. Confirm the Flask route accepts
a JSON body and returns `content_id` in the response.

---

### Milestone 4 — Signals 2 & 3 + Confidence Scoring

**Spec sections to provide:** Detection Signals (Signals 2 & 3) + Confidence Scoring section + diagram

**What to ask the AI tool to generate:**
"Generate: (1) `get_stylometric_score()` measuring sentence length variance, type-token
ratio, filler word density, and average sentence length — returning `(float, dict)`;
(2) `get_burstiness_score()` measuring CV, outlier ratio, max/min ratio, and sequential
diff — returning `(float, dict)`;
(3) `compute_confidence()` applying the 0.50/0.30/0.20 weights and thresholds of 0.70/0.40."

**How to verify the output:**
Run all four calibration inputs from the spec. Confirm:
- Clearly AI input scores >= 0.70
- Clearly human input scores < 0.40
- Both borderline inputs land in 0.40–0.70
Print individual signal scores separately to identify any misbehaving signal.

---

### Milestone 5 — Production Layer

**Spec sections to provide:** Transparency Label Variants + Appeals Workflow + Architecture diagram

**What to ask the AI tool to generate:**
"Generate: (1) `generate_label()` mapping confidence scores to the exact three label variants
specified; (2) `POST /appeal` endpoint that validates input, updates status to `under_review`,
logs the appeal, and returns confirmation; (3) `POST /verify/<content_id>` that checks
eligibility, issues a `CERT-XXXXXXXX` certificate, updates status to `verified`."

**How to verify the output:**
- Manually test that all three label variants are reachable by submitting inputs at different
  confidence levels.
- Submit an appeal and confirm `GET /log` shows `"status": "under_review"` and
  `appeal_reasoning` populated.
- Request a certificate on a `likely_human` submission and confirm `GET /log` shows
  `"status": "verified"` and `certificate_id` populated.

---

## Stretch Features Plan

### Stretch 1 — Ensemble Detection
Implemented as Signal 3 (Burstiness) with documented weights: LLM 50%, Stylometric 30%,
Burstiness 20%. The `compute_confidence()` function in `scoring.py` implements the weighted
average. Weights and reasoning are documented in the Confidence Scoring section above and
in `scoring.py` inline comments.

### Stretch 2 — Provenance Certificate
`POST /verify/<content_id>` — available to creators of `likely_human` or `under_review`
submissions. Issues a unique `CERT-XXXXXXXX` ID stored in a `certificates` SQLite table.
Updates the submission status to `"verified"`. Badge display format:
`"✅ Verified Human | Certificate #CERT-XXXXXXXX | Issued YYYY-MM-DD"`
The certificate is visible in `GET /log` as a `certificate_id` field.

### Stretch 3 — Analytics Dashboard
`GET /dashboard` returns:
- Total submissions
- Breakdown by attribution (`likely_ai`, `uncertain`, `likely_human`) with count and average
  confidence per category
- Total appeals and appeal rate (appeals / total submissions)
- Total provenance certificates issued
- 7-day submission trend (count per day)

### Stretch 4 — Multi-Modal Support
`POST /submit` accepts an optional `content_type` field: `"text"` (default) or
`"image_description"`. When `content_type` is `"image_description"`, the LLM prompt frames
the classification specifically for image descriptions/captions rather than prose. Stylometric
and burstiness signals run unchanged on the text. This is a genuine second content type —
image metadata and alt-text generation is a common creative platform use case distinct from
prose writing.