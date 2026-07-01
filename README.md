# Provenance Guard

A backend system for AI-content attribution on creative writing platforms. Submit a piece of text and get back a structured classification, confidence score, and transparency label. Creators who disagree with a verdict can file an appeal.

**Portfolio walkthrough video:** https://www.loom.com/share/c0c0a4c69f774413a4a70600a714e456

---

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
# create .env with GROQ_API_KEY=your_key_here
python app.py
```

Endpoints: `POST /submit`, `POST /appeal`, `GET /log`, `GET /analytics`, `POST /verify`, `GET /certificate/<cert_id>`, `POST /submit/image`

**Gradio UI** (all features in browser):
```bash
python ui.py   # launches on http://localhost:7860
```

---

## Architecture

A submitted piece of text enters at `POST /submit`, passes through `pipeline.py` → `process_text_submission()`, which runs all three detection signals, combines them, classifies the result, and writes to the SQLite audit log. Both Flask (`app.py`) and the Gradio UI (`ui.py`) call this shared function — no duplicated pipeline logic.

Appeals enter at `POST /appeal`, check the audit trail, and flip `status` to `under_review`. Creators can then request a Verified Human certificate at `POST /verify`.

```
POST /submit
  → Input validation (400 on missing fields)
  → Rate limiter (429 when exceeded)
  → pipeline.process_text_submission(text, creator_id)
       → Signal 2: stylometric heuristics  [pure Python, always succeeds]
       → Signal 3: AI phrase density       [pure Python, always succeeds]
       → Signal 1: Groq LLM               [can 502 on network error]
       → combine_scores(0.50×s1 + 0.30×s2 + 0.20×s3) → raw_score
       → classify(raw_score) → attribution + confidence
       → generate_label() → label text
       → audit log write (SQLite, timeout=5)
  → JSON response: content_id, attribution, confidence, label

POST /appeal
  → Input validation (400)
  → get_submission(content_id) → 404 if not found
  → status check → 409 if already under_review
  → file_appeal() → UPDATE status + appeal fields
  → JSON response: confirmation

GET /log
  → get_log(limit) → all submission rows, newest first

GET /analytics
  → get_analytics() → totals, distribution, appeal_rate, signal_agreement_rate

POST /verify
  → get_submission(content_id) → 404 if not found
  → 403 if likely_ai with no appeal (must appeal first)
  → idempotent: returns existing cert if already issued
  → issue_certificate() → cert_id
  → JSON response: cert_id, content_id, issued_at

GET /certificate/<cert_id>
  → get_certificate(cert_id) → 404 if not found
  → JSON: cert_id, content_id, creator_id, issued_at, statement, attribution, confidence

POST /submit/image
  → Input validation (400 on missing fields)
  → Rate limiter (same as /submit)
  → Signal 1 only: get_image_llm_score(image_b64, media_type)
  → classify(signal_1_score) → attribution + confidence
  → audit log write (content_type="image", s2/s3=null)
  → JSON response: content_id, attribution, confidence, label, note
```

---

## Detection Signals

### Signal 1 — Groq LLM (`llama-3.3-70b-versatile`)

**What it measures:** Semantic and stylistic coherence holistically — does this text *feel* AI-generated? The model picks up on balanced sentence structure, hedging phrases ("it is important to note that…", "furthermore"), lack of genuine personal voice, and the unnervingly consistent tone that characterizes most LLM output.

**Why it differs between human and AI writing:** AI models are trained to produce safe, well-structured, consistently-toned output. Human writing carries idiosyncratic word choices, emotional inconsistency, tangents, and uneven rhythm that reflects a real person's thought process. An LLM evaluator detects the absence of those irregularities even when it cannot articulate exactly why — the same way a person can often tell something is "off" before they can explain it.

**Why I chose it:** It captures what stylometric numbers cannot: the felt quality of the writing, the presence or absence of a genuine voice. No amount of sentence-length variance calculation tells you whether the text contains a real person's tangential observation. The Groq free tier makes it cost-free, and `temperature=0` gives deterministic output.

**Blind spots:** A skilled human deliberately writing in a formal structured style will score high. Heavily edited AI output that has rough edges added back in will score low. It adds 1–2 seconds of latency to every request and is the only component that can fail with a network error.

**Output:** `signal_1_score` — float 0.0–1.0 (1.0 = highly AI-like), or `None` on parse failure.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Statistical structural patterns computed directly from the text. Three sub-metrics, each normalized to 0.0–1.0:

| Sub-metric | AI direction | Why |
|---|---|---|
| Sentence-length variance | Low variance | AI sentences cluster around a predictable length; human sentences jump from fragments to compound run-ons |
| Type-token ratio (TTR) | Low TTR | AI text tends toward consistent formal vocabulary; human text is more lexically varied |
| Punctuation density | Low density | AI uses standard minimal punctuation; human writing includes stylistic em dashes, ellipses, informal omissions |

`signal_2_score = mean(sentence_variance_score, ttr_score, punctuation_score)`

**Why it differs:** AI text is structurally uniform — sentence lengths cluster around a predictable range and punctuation follows standard patterns. Human writing is messier. A one-word exclamation followed by a 35-word compound sentence is human. Every sentence being 14–18 words is suspicious.

**Why I chose it:** It is structurally independent from Signal 1. Signal 1 is semantic; Signal 2 is statistical. They can disagree. When they do, the confidence score reflects that disagreement and the system hedges toward uncertain. When they agree, the combined score reflects that consensus. This independence is the core justification for multi-signal detection.

**Reliability note:** Sentence-length variance is the dominant sub-metric. TTR is the weakest: for texts under ~80 words, TTR saturates toward 1.0 regardless of authorship (most words in a short passage are unique), so it can mildly mis-vote on short AI text. It is included per spec and acts as a tiebreaker at the margin. Punctuation density is similarly compressed on typical documents where both AI and human writing sit in the 1–5% density range.

**Blind spots:** Formal or academic human writing (legal documents, technical tutorials) is structurally uniform and can look AI-like to this signal. Short texts (fewer than 3 sentences) produce unreliable variance estimates — the function returns a neutral 0.5 for that sub-metric when insufficient data exists.

**Output:** `signal_2_score` — float 0.0–1.0 (1.0 = most AI-like), always succeeds.

---

### Signal 3 — AI Phrase Density (pure Python)

**What it measures:** Lexical patterns — how often the text uses phrases disproportionately common in LLM output (e.g. "it is important to note", "furthermore", "paradigm shift", "delve into", "stakeholders"). Frequency is computed per 1000 words and normalized against a `MAX_DENSITY` of 5.0 phrases per 1000 words.

**Why it differs:** AI models are trained on each other's output and on human feedback that rewards certain formal, hedge-heavy phrasings. These phrases cluster in AI writing at rates rarely seen in unassisted human text. A single occurrence is unremarkable; three or four in a 200-word passage is a strong signal.

**Why I chose it:** Lexically independent from both Signal 1 (which judges voice and coherence holistically) and Signal 2 (which measures sentence structure statistics). Signal 1's system prompt was explicitly cleaned of phrase enumeration so that Signal 1 and Signal 3 do not overlap — Signal 1 judges the *feel* of the writing, Signal 3 counts the *words*.

**Reliability note:** This is the **weakest of the three signals**. It is trivially gameable — an AI output with the same phrases swapped out would score 0.0. The phrase list is static; new AI writing patterns go undetected until the list is updated. It serves as a complementary tiebreaker at the margin, not a primary classifier. Signal 1 remains dominant at 50% weight.

**Short-text guard:** If fewer than 10 words, returns 0.5 (neutral) — consistent with the Signal 1 and Signal 2 guards.

**Output:** `signal_3_score` — float 0.0–1.0 (1.0 = phrase density ≥ 5 per 1000 words), always succeeds. `null` in the audit log for image submissions and pre-ensemble rows.

---

## Confidence Scoring

### Combination formula

```
raw_score = (0.50 × signal_1_score) + (0.30 × signal_2_score) + (0.20 × signal_3_score)
confidence = 0.5 + |raw_score − 0.5|

if raw_score >= 0.70  → attribution = "likely_ai"
elif raw_score <= 0.30 → attribution = "likely_human"
else                   → attribution = "uncertain"
```

Signal 1 is weighted 0.50 — it captures semantic meaning, the thing both other signals are blind to. Signal 2 is weighted 0.30 — structurally independent, three distinct sub-metrics. Signal 3 is weighted 0.20 — a useful lexical tiebreaker but gameable and static, so it carries the least weight. The confidence formula maps any `raw_score` to a [0.5, 1.0] certainty value — it measures how strongly the signals agree, not which direction they point.

The uncertain band (0.30–0.70) is intentionally wide. A false positive — labeling a human's work as AI — is worse than a false negative on a writing platform.

### Short-text guard (all three signals)

All three signals return **0.5 (neutral)** when the input is below their minimum data threshold:

| Signal | Guard condition |
|---|---|
| Signal 1 (LLM) | fewer than 10 words — LLM judgment is unreliable with no context |
| Signal 2 (stylometrics) | sub-metric guards: < 3 sentences for variance, < 20 tokens for TTR |
| Signal 3 (phrase density) | fewer than 10 words |

This means very short inputs (a single phrase, a title, two words) consistently produce `uncertain` rather than a confident misclassification. Without these guards, the LLM in particular would assign high AI scores to inputs like "sub dude" — two words with no linguistic signal — simply because there is nothing human-like to detect.

### Validated examples

**High-confidence case — clearly AI-generated text:**

> *"Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."*

| Signal 1 | Signal 2 | Signal 3 | raw_score | attribution | confidence |
|---|---|---|---|---|---|
| 0.850 | 0.554 | 1.000 | 0.791 | `likely_ai` | **0.7912 (79%)** |

All three signals agree. Signal 1 detects uniform formal tone. Signal 2 detects low sentence-length variance. Signal 3 scores 1.0 — the text contains "paradigm shift", "it is important to note", "furthermore", and "stakeholders", saturating the phrase density cap.

**Lower-confidence case — borderline text:**

> *"I've been thinking a lot about remote work lately. There are genuine tradeoffs — flexibility and no commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity varies widely by individual and role type."*

| Signal 1 | Signal 2 | Signal 3 | raw_score | attribution | confidence |
|---|---|---|---|---|---|
| 0.420 | 0.536 | 0.000 | 0.371 | `uncertain` | **0.6290 (63%)** |

Signal 1 reads the personal opener as human-leaning. Signal 2 sees moderately uniform structure. Signal 3 scores 0.0 — no AI phrases present. Signals partially conflict — correctly reports uncertainty.

---

## Transparency Labels

Labels are selected by **attribution alone** — no confidence gate — so all three variants are always reachable regardless of confidence value.

**High-confidence AI** (`attribution == "likely_ai"`):

> "This content shows strong signals of AI generation. Our automated analysis found the writing style and structure consistent with AI-generated text (confidence: 73%). If you are the creator and believe this is incorrect, you can submit an appeal."

**High-confidence Human** (`attribution == "likely_human"`):

> "This content shows strong signals of human authorship. Our analysis found the writing style, structure, and patterns consistent with human-written work (confidence: 73%)."

**Uncertain** (`attribution == "uncertain"`):

> "Our system could not confidently determine whether this content was written by a human or generated by AI. The signals were mixed or inconclusive. This label reflects genuine uncertainty — not an accusation. If you are the creator, you may submit an appeal to add context."

The confidence percentage appears on the AI and Human labels and is **intentionally omitted** from the Uncertain label. Displaying a number on an uncertain verdict ("62% confident I'm not sure") is confusing to a non-technical reader and implies false precision. The raw value is stored in the audit log for reviewer use.

---

## Rate Limiting

**Limits:** `10 requests per minute; 100 requests per day` on `POST /submit`.

**Reasoning:** A real writer submitting work might submit 3–5 pieces in a session, with occasional bursts up to 10 if they are iterating on drafts. The 10/minute ceiling stops scripted flooding while leaving room for legitimate usage bursts. The 100/day ceiling gives heavy users a generous daily allowance while blocking automated scraping or adversarial probing of the classifier (which would otherwise allow someone to reverse-engineer the signal thresholds for free by submitting thousands of variants).

`POST /appeal` is not rate-limited: unknown `content_id` values hit the 404 guard immediately (no meaningful work is done), and repeat appeals on valid content IDs are blocked by the already-under-review guard (409). Together these make flooding `/appeal` either pointless or self-limiting.

**Verified behavior** (12 rapid requests):

```
200  200  200  200  200  200  200  200  200  200  429  429
```

Requests 1–10 succeed; requests 11–12 return HTTP 429.

---

## Appeals Workflow

Any creator who holds a `content_id` from a `/submit` response can file an appeal:

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{
    "content_id": "a89e3bbc-5176-46ef-b005-d5f840a80e61",
    "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
  }'
```

Response:
```json
{
  "content_id": "a89e3bbc-5176-46ef-b005-d5f840a80e61",
  "status": "under_review",
  "message": "Your appeal has been received and is under review."
}
```

The system:
1. Looks up `content_id` → 404 if not found
2. Checks current status → 409 if already `under_review` (protects original appeal reasoning from silent overwrite)
3. Updates `status` to `under_review`, writes `appeal_reasoning` and `appeal_timestamp` into the audit row
4. Returns confirmation

No automated re-classification occurs. A human reviewer reads the queue via `GET /log`.

---

## Audit Log

`GET /log?limit=N` returns the N most recent submissions as structured JSON (default 20). Every row includes both signal scores, the combined confidence, the label text shown to the reader, the current status, and — if an appeal was filed — the creator's reasoning and the appeal timestamp.

### Sample entries (3 post-MS5 submissions)

**Entry 1 — `likely_ai`, appealed (`under_review`)**
```json
{
  "content_id":       "a89e3bbc-5176-46ef-b005-d5f840a80e61",
  "creator_id":       "ms5-test",
  "timestamp":        "2026-06-29T08:10:11.165030+00:00",
  "signal_1_score":   0.85,
  "signal_2_score":   0.554,
  "attribution":      "likely_ai",
  "confidence":       0.7316,
  "label":            "This content shows strong signals of AI generation. Our automated analysis found the writing style and structure consistent with AI-generated text (confidence: 73%). If you are the creator and believe this is incorrect, you can submit an appeal.",
  "status":           "under_review",
  "notes":            null,
  "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
  "appeal_timestamp": "2026-06-29T08:10:23.433838+00:00"
}
```

**Entry 2 — `likely_human`, no appeal**
```json
{
  "content_id":       "21817ae3-ae52-4740-8746-93cb01a5609a",
  "creator_id":       "ms5-test",
  "timestamp":        "2026-06-29T08:10:11.982300+00:00",
  "signal_1_score":   0.12,
  "signal_2_score":   0.5008,
  "attribution":      "likely_human",
  "confidence":       0.7277,
  "label":            "This content shows strong signals of human authorship. Our analysis found the writing style, structure, and patterns consistent with human-written work (confidence: 73%).",
  "status":           "classified",
  "notes":            null,
  "appeal_reasoning": null,
  "appeal_timestamp": null
}
```

**Entry 3 — `uncertain`, no appeal**
```json
{
  "content_id":       "476cf50b-ac57-4c8e-a966-645edc37a946",
  "creator_id":       "ms5-test",
  "timestamp":        "2026-06-29T08:10:12.435505+00:00",
  "signal_1_score":   0.3,
  "signal_2_score":   0.5229,
  "attribution":      "uncertain",
  "confidence":       0.6108,
  "label":            "Our system could not confidently determine whether this content was written by a human or generated by AI. The signals were mixed or inconclusive. This label reflects genuine uncertainty — not an accusation. If you are the creator, you may submit an appeal to add context.",
  "status":           "classified",
  "notes":            null,
  "appeal_reasoning": null,
  "appeal_timestamp": null
}
```

---

## Known Limitations

### 1. Formal and technical human writing (high false-positive risk)

A software developer writing a detailed technical walkthrough — formal register, precise vocabulary, consistent sentence structure, no slang — will often score high on both signals. Signal 2 scores it AI-like on structure (low sentence-length variance, standard punctuation). Signal 1 may also score it AI-like (well-organized, no personal tangents, no hedging). The system can return `likely_ai` for a fully human piece. This is not a calibration problem that more data solves — it is a property of the signals: they measure regularities, and expert human writing in a formal domain is genuinely regular. The appeal path is the designed remedy, not a fallback.

This risk is asymmetric by content type: a casual blog post is almost never misclassified; a technical tutorial or legal memo has materially higher false-positive probability.

### 2. Short texts (fewer than 3–5 sentences)

Signal 2 becomes unreliable for very short texts — you cannot compute meaningful sentence-length variance from two data points. Signal 1 has the same problem: given only a few words, the LLM has no linguistic signal to work with and produces arbitrary scores. Without guards, both signals would confidently misclassify short inputs.

All three signals now return neutral 0.5 below their minimum data thresholds (see Confidence Scoring → Short-text guard above). For very short inputs this means the combined score sits near 0.5 → `uncertain`, which is the honest answer. The multi-signal guarantee still does not fully hold for short texts — all signals are returning guard values rather than real measurements — but the system no longer produces false high-confidence verdicts on degenerate inputs.

### 3. Image classification is single-signal

`POST /submit/image` runs Signal 1 (Groq vision) only. Stylometrics and phrase density have no image equivalent. Image AI-detection is inherently less reliable than text detection, and the label and appeal path work the same way — but treat image verdicts with more caution than text verdicts.

---

## Analytics

`GET /analytics` returns aggregate metrics across all submissions:

```json
{
  "total_submissions": 22,
  "attribution_distribution": {"likely_ai": 14, "likely_human": 4, "uncertain": 4},
  "appeal_rate": 0.0455,
  "signal_agreement_rate": 0.4545
}
```

**Signal agreement rate** measures the fraction of dual-signal submissions where Signal 1 and Signal 2 vote in the same direction. Rows where `signal_2_score == 0.5` are excluded — that is the neutral guard value, not a real vote. This metric demonstrates the value of multi-signal design: when both signals agree, the combined verdict is reliable; when they disagree, `uncertain` is the correct call.

---

## Provenance Certificate

Creators can earn a "Verified Human" credential after a positive classification or a successful appeal.

**Issue a certificate:**
```bash
curl -s -X POST http://localhost:5000/verify \
  -H "Content-Type: application/json" \
  -d '{"content_id": "<id>", "verification_statement": "I wrote this myself."}'
```

Response:
```json
{
  "cert_id": "56a7fae8-7b57-4b86-909a-a802338fce01",
  "content_id": "<id>",
  "issued_at": "2026-06-29T09:04:44.950525+00:00",
  "message": "Verified Human credential issued."
}
```

**Retrieve a certificate:**
```bash
curl http://localhost:5000/certificate/56a7fae8-7b57-4b86-909a-a802338fce01
```

**Gate logic:**
- `likely_human` → certificate issued immediately
- `likely_ai` with no appeal → **403**: must file an appeal first
- `likely_ai` with status `under_review` → certificate issued (appeal IS the authorship assertion)
- Already certified → returns the existing certificate (idempotent — same `cert_id` every time)

The appeal-first gate for `likely_ai` submissions is consistent with the false-positive philosophy: it does not make certification harder for a wrongly-flagged creator — it requires them to formally assert their authorship via the appeal, which creates the documentation record. Certification then seals that record with a credential.

---

## Multi-modal Support

`POST /submit/image` accepts a base64-encoded image and assesses whether it appears AI-generated using the Groq vision model (`meta-llama/llama-4-scout-17b-16e-instruct`).

```bash
curl -s -X POST http://localhost:5000/submit/image \
  -H "Content-Type: application/json" \
  -d '{"image_b64": "<base64>", "media_type": "image/jpeg", "creator_id": "creator1"}'
```

Only Signal 1 applies — stylometrics and phrase density have no image equivalent. The response includes a `note` field documenting this limitation. The label, appeal path, and certificate path work identically to text submissions.

---

## Gradio UI

```bash
python ui.py
```

Launches on `http://localhost:7860`. Six tabs:

| Tab | What it does |
|---|---|
| Analyze Text | Text + creator_id → label + full signal breakdown |
| Analyze Image | Image upload + creator_id → image classification result |
| File Appeal | content_id + reasoning → appeal confirmation |
| Verify Human | content_id + statement → certificate or rejection reason |
| Analytics | Refresh button → totals, distribution, appeal rate, signal agreement |
| View Log | Limit selector → DataFrame of recent audit entries |

`ui.py` calls `pipeline.process_text_submission()` directly — the same function Flask uses. No duplicated pipeline logic, no HTTP round-trip. Both Flask (`:5000`) and Gradio (`:7860`) share `provenance.db` with `sqlite3.connect(timeout=5)` to prevent lock contention.

---

## Spec Reflection

**One way the spec guided implementation:** The false-positive trace in MS1 — tracing what happens when a non-native English speaker submits a formal poem — forced an early decision that the "uncertain" band must be wide. This was a design decision before any code existed. The resulting thresholds (0.30/0.70) were set because of that trace, not tuned empirically afterward. Without the trace, I would have defaulted to a symmetric 0.40/0.60 split, which would have pushed more borderline cases into accusations.

**One way implementation diverged from the spec:** The spec described punctuation density as one of three comparable sub-metrics in Signal 2. In practice it barely differentiates AI from human text at typical document lengths: both AI and human writing sit in the 1–5% punctuation-density range (periods end every sentence regardless of author). The metric compresses into a narrow high-score band for almost every input, contributing very little to the combined signal_2_score. If deploying this for real, I would replace raw punctuation density with a count of *expressive* punctuation specifically — em dashes, ellipses, repeated exclamation marks, question marks mid-sentence — which is what the planning document was actually trying to capture. I kept the metric as specified rather than diverging mid-build, and documented the limitation in both the code and this README.

---

## AI Usage

**Instance 1 — Groq LLM signal function (`get_llm_score`)**

I directed the AI to generate a function that calls the Groq API with a structured prompt and returns a single float. The AI produced a version that called the API correctly but treated any non-2xx API response or malformed output as a raised exception. I revised this in two ways: (1) I separated network/API errors (which should raise and produce a 502 at the route level) from *parse* failures where the API responded but the model output was malformed JSON — for parse failures, I changed the function to return `None` and log a warning, while the route defaults to `uncertain` and still writes an audit entry. This matters because a parse failure is a recoverable soft error, not a service outage. (2) I added `temperature=0` and `max_tokens=20` — the AI's initial version omitted both, which would produce nondeterministic output on retries and waste tokens on verbose model preambles.

**Instance 2 — `POST /appeal` endpoint**

I directed the AI to generate the appeal endpoint per the spec: accept `content_id` and `creator_reasoning`, update the status, log the appeal, return a confirmation. The AI generated a version that called `file_appeal()` unconditionally after the 404 check — if a creator submitted two appeals, the second would silently overwrite the first appeal's reasoning in the database. I added a status pre-check that returns 409 if the submission is already `under_review`, with the reasoning that an audit log must be append-friendly, not destructive. The original creator's reasoning is the primary piece of evidence a human reviewer needs; overwriting it on a re-submission defeats the purpose of logging it at all.
