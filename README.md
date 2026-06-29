# Provenance Guard

A backend system for AI-content attribution on creative writing platforms. Submit a piece of text and get back a structured classification, confidence score, and transparency label. Creators who disagree with a verdict can file an appeal.

---

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
# create .env with GROQ_API_KEY=your_key_here
python app.py
```

Endpoints: `POST /submit`, `POST /appeal`, `GET /log`

---

## Architecture

A submitted piece of text enters at `POST /submit`, passes input validation and rate limiting, then flows through both detection signals. Signal 2 (stylometrics) runs first because it is pure Python and always succeeds; Signal 1 (Groq LLM) runs second because it can fail with a network error. Both scores feed the confidence scorer, the label generator selects the correct label text, and the full result is written to the SQLite audit log before the JSON response is returned.

Appeals enter at `POST /appeal`, look up the original decision by `content_id`, check that the submission is not already under review (to protect the audit trail), then flip `status` to `under_review` and write the creator's reasoning into the same audit row.

```
POST /submit
  → Input validation (400 on missing fields)
  → Rate limiter (429 when exceeded)
  → Signal 2: stylometric heuristics  [pure Python, never fails]
  → Signal 1: Groq LLM                [can 502 on network error]
  → combine_scores(0.60×s1 + 0.40×s2) → raw_score
  → classify(raw_score) → attribution + confidence
  → generate_label(attribution, confidence) → label text
  → audit log write (SQLite)
  → JSON response: content_id, attribution, confidence, label

POST /appeal
  → Input validation (400)
  → get_submission(content_id) → 404 if not found
  → status check → 409 if already under_review
  → file_appeal() → UPDATE status + appeal fields
  → JSON response: confirmation

GET /log
  → get_log(limit) → all submission rows, newest first
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

## Confidence Scoring

### Combination formula

```
raw_score = (0.60 × signal_1_score) + (0.40 × signal_2_score)
confidence = 0.5 + |raw_score − 0.5|

if raw_score >= 0.70  → attribution = "likely_ai"
elif raw_score <= 0.30 → attribution = "likely_human"
else                   → attribution = "uncertain"
```

Signal 1 is weighted 0.60 because it captures semantic meaning — the thing Signal 2 is entirely blind to. Signal 2 is weighted 0.40 because it is structurally independent and cheap to compute. The confidence formula maps any `raw_score` to a [0.5, 1.0] certainty value: 0.5 means the system genuinely cannot tell; 1.0 means both signals strongly agree. The same high confidence value can represent a strong AI verdict or a strong human verdict — it measures agreement, not direction.

The uncertain band (0.30–0.70) is intentionally wide. A false positive — labeling a human's work as AI — is worse than a false negative on a writing platform. Forcing borderline cases into accusations is the wrong failure mode.

### Validated examples

**High-confidence case — clearly AI-generated text:**

> *"Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."*

| Signal 1 | Signal 2 | raw_score | attribution | confidence |
|---|---|---|---|---|
| 0.850 | 0.554 | 0.732 | `likely_ai` | **0.7316 (73%)** |

Both signals agree strongly. Signal 1 detects the hedging language and uniform formal tone. Signal 2 detects low sentence-length variance (all three sentences are similar length).

**Lower-confidence case — borderline text (lightly edited AI output):**

> *"I've been thinking a lot about remote work lately. There are genuine tradeoffs — flexibility and no commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity varies widely by individual and role type."*

| Signal 1 | Signal 2 | raw_score | attribution | confidence |
|---|---|---|---|---|
| 0.300 | 0.536 | 0.395 | `uncertain` | **0.6108 (61%)** |

Signal 1 reads the personal opener ("I've been thinking") as human-leaning. Signal 2 sees moderately uniform structure and pulls AI-ward. The signals partially conflict — the system correctly reports uncertainty rather than committing to a verdict.

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

Signal 2 becomes unreliable for very short texts. You cannot compute meaningful sentence-length variance from two data points. The function returns a neutral 0.5 for that sub-metric when fewer than 3 sentences are detected, which means the system is effectively running on Signal 1 alone for haiku, single-paragraph excerpts, or short social media posts. The multi-signal guarantee does not hold for these inputs, and confidence scores may be misleadingly high because Signal 1 can be quite confident even when the structural evidence is absent.

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
