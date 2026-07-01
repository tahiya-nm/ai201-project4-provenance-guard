# Provenance Guard

A Flask backend system for AI content attribution on creative sharing platforms. Provenance Guard classifies submitted text as AI-generated or human-written using a three-signal ensemble pipeline, surfaces a transparency label to users, handles creator appeals, issues provenance certificates, enforces rate limiting, and exposes a structured audit log and analytics dashboard.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Detection Signals](#detection-signals)
- [Confidence Scoring](#confidence-scoring)
- [Transparency Label Variants](#transparency-label-variants)
- [Rate Limiting](#rate-limiting)
- [Stretch Features](#stretch-features)
- [Audit Log Sample](#audit-log-sample)
- [Known Limitations](#known-limitations)
- [Spec Reflection](#spec-reflection)
- [AI Usage](#ai-usage)
- [Setup](#setup)
- [Endpoints](#endpoints)

---

## Architecture Overview

A piece of content enters via `POST /submit` with a `text` field, a `creator_id`, and an optional `content_type`. The input is validated (minimum length, required fields). It is then passed to three detection signals that run independently:

- **Signal 1** (LLM/Groq) assesses whether the text semantically reads as AI-generated
- **Signal 2** (Stylometrics) measures global statistical properties of the writing
- **Signal 3** (Burstiness) measures how much writing rhythm varies locally from sentence to sentence

Each signal returns a score from `0.0` (human-like) to `1.0` (AI-like). The three scores are combined into a single weighted ensemble confidence score. That score maps to an attribution category and transparency label. The full result is written to a SQLite audit log and returned in the JSON response. The `content_id` in the response is what a creator uses to file an appeal or request a provenance certificate.

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
              Lookup content_id → 404 if not found
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

---

## Detection Signals

### Signal 1 — LLM Classification (Groq: llama-3.3-70b-versatile)

**What it measures:** Semantic and stylistic coherence holistically. The model reads the text as a human would and assesses whether it reads like AI — capturing meaning-level patterns like hedged language, overly balanced structure, unnatural polish, and absence of idiosyncratic personal voice. This is the only signal with semantic understanding.

**Why chosen:** No statistical heuristic can replicate semantic comprehension. The LLM recognizes patterns like "Furthermore, stakeholders across various sectors must collaborate..." as characteristic AI phrasing in a way that sentence length measurements cannot.

**What it misses:** Short texts (< 50 words) give insufficient context for reliable assessment. Intentionally humanized AI output (added typos, colloquialisms) can reduce its confidence. Very formal human writing may score higher than expected.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Global statistical properties of the text as a whole — four sub-features combined via weighted sum:

1. **Sentence length uniformity** (35% weight) — AI text has consistent sentence lengths (low standard deviation). Human text mixes short punchy sentences with long elaborations.
2. **Type-token ratio (TTR)** (25% weight) — vocabulary diversity (unique words / total words). Very high TTR (> 0.85) is typically human; AI produces consistent moderate TTR.
3. **Filler/informal word density** (20% weight) — words like "just", "honestly", "like", "basically". Humans use these naturally; AI avoids them in formal contexts.
4. **Average sentence length** (20% weight) — AI tends toward longer, denser sentences on average.

**Why chosen:** Stylometric analysis is well-established in authorship attribution research. It provides structural grounding independent of any LLM behavior, making the ensemble more robust — two independent failure modes rather than one.

**What it misses:** Academic or formal human writing scores falsely high (long sentences, no fillers, formal vocabulary — all AI-like signals despite being human). Non-native English speakers who write formally are similarly affected. TTR reliability drops on longer texts.

---

### Signal 3 — Burstiness Analysis (pure Python — Stretch: Ensemble)

**What it measures:** Local sequential variation patterns — how much sentence lengths change from one sentence to the next. Human writing is "bursty": it alternates between short punchy sentences and long elaborative ones. AI maintains rhythmic consistency even within paragraphs.

Four sub-features:
1. **Coefficient of variation (CV)** (30% weight) — std/mean of sentence lengths. Low CV = uniform rhythm = AI-like.
2. **Outlier sentence ratio** (25% weight) — proportion of very short (< 4 words) or very long (> 35 words) sentences mixed together. Humans mix dramatic extremes; AI avoids them.
3. **Max-to-min sentence length ratio** (25% weight) — wide range = bursty = human-like.
4. **Average sequential difference** (20% weight) — mean of |length[i] - length[i-1]|. Low diff = smooth, non-bursty rhythm = AI-like.

**Why it is distinct from Signal 2:** Signal 2 measures the *shape of the overall distribution* across the whole text. Signal 3 measures *sequential change patterns* — how much each sentence differs from its neighbor. These are mathematically independent: a text can have high global variance but low sequential variation (or vice versa). A long academic paper might have consistent paragraph structure (low burstiness) despite global length variation.

**Why chosen:** Adds a genuinely different axis to the ensemble that neither the LLM nor stylometrics captures. The ramen review example (`sentence_length_std: 13.38`, `avg_sequential_length_diff: 20.0`) shows how dramatically bursty real human writing can be.

**What it misses:** Poetry and intentionally rhythmic structured writing scores as AI-like. Texts shorter than 3 sentences fall back to a neutral 0.5 score. Dialogue-heavy writing may score as unexpectedly human.

---

## Confidence Scoring

### Ensemble Formula

```
final_confidence = (llm_score × 0.50) + (stylometric_score × 0.30) + (burstiness_score × 0.20)
```

**Weight reasoning:**
- **LLM (50%)** — most reliable; has semantic understanding no statistical heuristic can replicate
- **Stylometric (30%)** — well-established structural signal; provides grounding independent of LLM
- **Burstiness (20%)** — useful additional axis but noisier, especially on short texts

### Decision Thresholds

```
confidence >= 0.70  →  likely_ai      (require strong signal before labeling AI-generated)
0.40 <= conf < 0.70 →  uncertain      (wide band — benefit of the doubt)
confidence <  0.40  →  likely_human
```

**Why these thresholds:** A false positive — labeling a human writer's work as AI-generated — is significantly more harmful than a false negative on a creative platform. Setting the `likely_ai` threshold at 0.70 (not 0.50) means the system requires all three signals to lean strongly AI before making an accusation. The uncertain band is intentionally wide (30 percentage points), giving creators meaningful breathing room. A score of 0.60 means the system leans AI but cannot assert it — the creator is not penalized.

### How Calibration Was Validated

Four inputs spanning the confidence range were tested, comparing expected versus actual results:

| Input type | Expected | Actual confidence | Attribution |
|------------|----------|-------------------|-------------|
| Classic AI phrasing | ≥ 0.70 | **0.734** | `likely_ai` ✅ |
| Casual human writing | < 0.40 | **0.106** | `likely_human` ✅ |
| Formal human writing | 0.40–0.70 | **0.668** | `uncertain` ✅ |
| AI image description | ≥ 0.70 | **0.742** | `likely_ai` ✅ |

All four matched their expected ranges. The scores are meaningfully separated — the human example (0.106) and AI example (0.734) are 0.628 apart, not clustered near 0.5.

### Example 1 — High-Confidence AI (confidence: 0.734)

**Text submitted:**
> "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."

**Signal scores:**
- `llm_score: 0.9` — LLM flagged polished tone, formal vocabulary, balanced structure
- `stylometric_score: 0.525` — zero filler words, uniformity score 0.547
- `burstiness_score: 0.633` — low sequential variation (avg diff: 11.5 words)
- **Ensemble: `0.734` → `likely_ai`**

---

### Example 2 — High-Confidence Human (confidence: 0.106)

**Text submitted:**
> "honestly i dont even know where to start with this. my sister called me at like 11pm completely freaking out about some drama with her roommate and i just sat there on my couch half asleep trying to be supportive while also desperately wanting to go to bed. she does this every single time. love her to death but wow."

**Signal scores:**
- `llm_score: 0.0` — LLM identified colloquialisms, personal anecdote, irregular rhythm
- `stylometric_score: 0.243` — filler words present, sentence_length_std of 13.38 (very variable)
- `burstiness_score: 0.164` — highly bursty (avg sequential diff: 20.0 words, CV: 0.892)
- **Ensemble: `0.106` → `likely_human`**

The contrast between these two examples is stark. The human example has a `sentence_length_std` of 13.38 versus the AI example's 5.44 — the human writing varies dramatically in rhythm (short "love her to death but wow." next to a 37-word sentence), while the AI writing is structurally consistent throughout.

---

## Transparency Label Variants

All three label variants are shown below exactly as they appear in API responses and would be displayed to users.

### Variant 1 — High-Confidence AI (confidence ≥ 0.70)

> ⚠️ Likely AI-Generated — This content shows strong indicators of AI generation (73% confidence). AI detection is not perfect. If this is your own original writing, you may submit an appeal.

*The confidence percentage reflects the ensemble score. The appeal option is always surfaced — the path forward is never hidden from the creator.*

---

### Variant 2 — Attribution Uncertain (0.40 ≤ confidence < 0.70)

> 🔍 Attribution Uncertain — Our system could not confidently determine the origin of this content (67% AI-leaning confidence). No action has been taken. This label is for informational purposes only.

*Uses "AI-leaning" phrasing to acknowledge the direction of uncertainty without making an accusation. Explicitly states no action has been taken.*

---

### Variant 3 — High-Confidence Human (confidence < 0.40)

> ✅ Appears Human-Written — This content shows strong indicators of human authorship (89% confidence). Thank you for contributing original work.

*Displays human confidence (100% − AI%) rather than the raw AI score. Affirms the creator positively rather than simply saying "not AI."*

---

**Design rationale:** The false positive problem shaped every label. The uncertain variant deliberately avoids penalty language. The AI variant surfaces the appeal option immediately. The threshold asymmetry (0.70, not 0.50) means the ⚠️ label is rare — it requires strong, consistent signal across all three independent detectors.

---

## Rate Limiting

**Limits applied to `POST /submit`:** `10 per minute; 100 per day`

**Reasoning:**
- **10 per minute:** A legitimate creator submitting their own work submits at most a few times per session. Ten requests per minute is already generous for manual use. A script flooding the system would exhaust this within seconds, providing effective burst protection.
- **100 per day:** Gives an active creator approximately 12 submissions per waking hour — more than enough for real use. Prevents anyone from running bulk classification campaigns against the system (e.g., scraping a platform's entire content library).

**Rate limit test evidence** (12 rapid requests — limit exceeded after 10):

```
201
201
201
201
201
201
201
201
201
201
429
429
```

The 429 response body:
```json
{
    "error": "Rate limit exceeded",
    "message": "Too many submissions. Limit: 10 per minute, 100 per day.",
    "hint": "Wait 60 seconds before retrying."
}
```

---

## Stretch Features

### Stretch 1 — Ensemble Detection (3 Signals)

The system uses three genuinely independent signals rather than two. Signal 3 (Burstiness) measures local sequential variation patterns — a different mathematical axis from Signal 2 (global distributions). The ensemble weights (50/30/20) and their rationale are documented in `scoring.py` and in the Confidence Scoring section above.

---

### Stretch 2 — Provenance Certificate

`POST /verify/<content_id>` issues a "Verified Human" credential to creators of content classified as `likely_human` or currently `under_review` (appealed).

**How it works:**
- Checks eligibility: blocks `likely_ai` content that hasn't been appealed
- Issues a unique `CERT-XXXXXXXX` ID stored in the `certificates` SQLite table
- Updates submission status to `"verified"`
- Returns a `badge_display` string for platform display

**Example response:**
```json
{
    "certificate_id": "CERT-8FD5FE97",
    "content_id": "236896d1-50ed-4599-8444-a1a94142756b",
    "creator_id": "calibration-test",
    "issued_at": "2026-07-01T04:09:27.029978+00:00",
    "badge_display": "✅ Verified Human | Certificate #CERT-8FD5FE97 | Issued 2026-07-01",
    "status": "verified",
    "message": "Provenance certificate issued. This content is marked as verified human-written."
}
```

The certificate is visible in `GET /log` as `certificate_id` and `certificate_issued_at` fields on the submission entry.

---

### Stretch 3 — Analytics Dashboard

`GET /dashboard` returns aggregated detection statistics:

```json
{
    "appeal_rate": 0.067,
    "by_attribution": [
        {"attribution": "likely_ai",    "avg_confidence": 0.744, "count": 12},
        {"attribution": "likely_human", "avg_confidence": 0.251, "count": 2},
        {"attribution": "uncertain",    "avg_confidence": 0.668, "count": 1}
    ],
    "recent_trend_7d": [
        {"count": 15, "day": "2026-07-01"}
    ],
    "total_appeals": 1,
    "total_certificates_issued": 1,
    "total_submissions": 15
}
```

Metrics surfaced: total submissions, per-attribution breakdown with average confidence, appeal count and rate, certificate count, and 7-day daily submission trend.

---

### Stretch 4 — Multi-Modal Support

`POST /submit` accepts an optional `content_type` field:
- `"text"` (default) — prose writing: poems, stories, blog posts, essays
- `"image_description"` — written descriptions or captions for images

When `content_type: "image_description"` is passed, the LLM prompt frames the classification specifically for image descriptions rather than prose. Stylometric and burstiness signals run unchanged on the text content.

**Example — image description submission:**
```json
{
    "text": "A serene mountain lake at golden hour, its mirror-like surface reflecting the snow-capped peaks above...",
    "creator_id": "calibration-test",
    "content_type": "image_description"
}
```
**Result:** `confidence: 0.742`, `attribution: likely_ai` — the composition language ("exceptional balance and color harmony") and polished structure were flagged by both the LLM and burstiness signals.

This is a genuine second content type: AI-generated image alt-text and descriptions are a common creative platform concern distinct from prose writing.

---

## Audit Log Sample

Output of `GET /log?limit=3` showing all three status types:

```json
{
    "count": 3,
    "entries": [
        {
            "attribution": "likely_human",
            "burstiness_score": 0.183,
            "certificate_id": "CERT-8FD5FE97",
            "certificate_issued_at": "2026-07-01T04:09:27.029978+00:00",
            "confidence": 0.151,
            "content_id": "236896d1-50ed-4599-8444-a1a94142756b",
            "content_type": "text",
            "creator_id": "calibration-test",
            "label_text": "✅ Appears Human-Written — This content shows strong indicators of human authorship (85% confidence). Thank you for contributing original work.",
            "llm_score": 0.0,
            "status": "verified",
            "stylometric_score": 0.381,
            "timestamp": "2026-07-01T04:05:27.249621+00:00",
            "appeal_id": null,
            "appeal_reasoning": null,
            "appeal_timestamp": null
        },
        {
            "attribution": "likely_ai",
            "burstiness_score": 0.633,
            "certificate_id": null,
            "certificate_issued_at": null,
            "confidence": 0.734,
            "content_id": "41f9b1e2-faba-49c5-bd56-2ca368ae5d3e",
            "content_type": "text",
            "creator_id": "calibration-test",
            "label_text": "⚠️ Likely AI-Generated — This content shows strong indicators of AI generation (73% confidence). AI detection is not perfect. If this is your own original writing, you may submit an appeal.",
            "llm_score": 0.9,
            "status": "under_review",
            "stylometric_score": 0.525,
            "timestamp": "2026-07-01T04:03:06.794483+00:00",
            "appeal_id": "d520504b-b572-476e-a945-d5652b44288c",
            "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical native English prose.",
            "appeal_timestamp": "2026-07-01T04:08:29.399532+00:00"
        },
        {
            "attribution": "uncertain",
            "burstiness_score": 0.5,
            "certificate_id": null,
            "certificate_issued_at": null,
            "confidence": 0.668,
            "content_id": "63a8f539-c53a-4755-922b-34598d8a84c4",
            "content_type": "text",
            "creator_id": "calibration-test",
            "label_text": "🔍 Attribution Uncertain — Our system could not confidently determine the origin of this content (67% AI-leaning confidence). No action has been taken. This label is for informational purposes only.",
            "llm_score": 0.8,
            "status": "classified",
            "stylometric_score": 0.559,
            "timestamp": "2026-07-01T04:05:44.828134+00:00",
            "appeal_id": null,
            "appeal_reasoning": null,
            "appeal_timestamp": null
        }
    ]
}
```

Three entries showing the full status lifecycle: `classified` → `under_review` (with appeal) → `verified` (with certificate).

---

## Known Limitations

### Limitation 1 — Formal human writing systematically scores higher

A non-native English speaker writing carefully in formal prose — no contractions, long sentences, precise vocabulary, no colloquialisms — will score high on the stylometric signal because those are exactly the features the signal associates with AI writing. If the LLM is also uncertain (polished prose without strong personal voice), the ensemble may land in `uncertain` or even `likely_ai`. This is a direct consequence of the stylometric signal's design: it uses informal markers as human signals, which means formal-but-human writing is penalized. The wide uncertain band and appeals workflow are the mitigations, but the limitation is real.

### Limitation 2 — Short texts degrade structural signal quality

The rate limit test text ("This is a test submission for rate limit testing purposes only.") is a single sentence. Burstiness fell back to `0.5` (needs 3+ sentences), stylometrics fell back to `0.5` (needs 2+ sentences and 10+ words), and only the LLM ran meaningfully — returning `1.0`, giving a `0.75` ensemble. The system still returned a reasonable verdict, but the confidence is driven entirely by one signal, which inflates certainty. Any submission under ~80 words should be treated as lower-quality evidence by a human reviewer.

### Limitation 3 — Intentionally humanized AI output

AI-generated text that has been edited to add typos, informal phrasing, contractions, and irregular sentence lengths will score significantly lower on stylometrics and burstiness. The LLM signal is more robust to this but can still be fooled by sophisticated prompt engineering. This is an unavoidable limitation of probabilistic detection — the system should be understood as a signal, not a verdict.

---

## Spec Reflection

**One way the spec helped:** The instruction to write out the three label variants verbatim in `planning.md` before building forced a UX decision that would otherwise have been deferred until late implementation. Deciding upfront that the uncertain variant should say "no action has been taken" (not "we couldn't tell") shaped how the threshold asymmetry was framed throughout the system — the label and the 0.70 threshold are consistent expressions of the same design principle.

**One way implementation diverged from the spec:** The planning document assumed Signal 3 (Burstiness) would be a meaningful differentiator across most inputs. In practice, many test inputs were too short (2 sentences) for burstiness to compute, and it fell back to `0.5` more often than expected — including the formal human writing test (Test C) and all rate limit test submissions. The signal works well on longer texts but contributes less than the planned 20% weight on short content. If deploying this for real, I would add a text length check and reduce the burstiness weight dynamically for inputs under 3 sentences, increasing the LLM weight proportionally.

---

## AI Usage

**Instance 1 — Generating the Flask app skeleton and Signal 1 function (Milestone 3)**

I provided the architecture diagram from `planning.md` and the Signal 1 description ("sends text to Groq, returns `ai_probability` float parsed from JSON"). The AI generated a Flask route stub and a `get_llm_score()` function. I revised the output in two ways: (1) the generated function returned the full parsed dict rather than just the float and reasoning tuple the spec required, so I restructured the return signature; (2) the error handling only caught `Exception` generically, so I added a specific `json.JSONDecodeError` catch to handle the model occasionally wrapping its response in markdown fences, which the generic handler would have missed.

**Instance 2 — Generating Signal 2 and Signal 3 (Milestone 4)**

I provided the stylometric and burstiness signal descriptions from `planning.md` along with the specific sub-features and weights I had designed. The AI generated both functions. I revised Signal 2's TTR scoring formula — the generated version mapped high TTR as strongly AI-like (inverted logic), when high TTR actually indicates diverse vocabulary and is a human signal. I also revised the burstiness outlier threshold in Signal 3: the generated version used `< 5 words` as the short sentence threshold, which would flag many legitimate short sentences. I changed it to `< 4 words` to reduce false burstiness on normal short sentences like "Wait." or "Yes, exactly."

---

## Setup

```bash
# Clone the repo
git clone https://github.com/tahiya-nm/ai201-project4-provenance-guard.git
cd ai201-project4-provenance-guard

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate   # Mac/Linux
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install -r requirements.txt

# Create .env with your Groq API key
cp .env.example .env
# Edit .env and add: GROQ_API_KEY=your_key_here

# Run the server
python app.py
```

The SQLite database (`provenance.db`) is created automatically on first run.

---

## Endpoints

| Method | Endpoint | Rate Limited | Description |
|--------|----------|-------------|-------------|
| `POST` | `/submit` | 10/min, 100/day | Classify content |
| `POST` | `/appeal` | No | Contest a classification |
| `POST` | `/verify/<content_id>` | No | Request provenance certificate |
| `GET` | `/log?limit=N` | No | View audit log (default 50 entries) |
| `GET` | `/dashboard` | No | Analytics dashboard |

### POST /submit

```bash
curl -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "your content here", "creator_id": "user-123"}'
```

Optional: `"content_type": "image_description"` for image caption classification.

### POST /appeal

```bash
curl -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "UUID-FROM-SUBMIT", "creator_reasoning": "Your explanation here"}'
```

### POST /verify/<content_id>

```bash
curl -X POST http://localhost:5000/verify/UUID-FROM-SUBMIT
```

Only available for `likely_human` or `under_review` submissions.