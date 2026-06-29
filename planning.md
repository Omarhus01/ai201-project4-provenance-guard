# Provenance Guard — Planning Document

## Artifact 1 — Architecture Narrative

A single piece of text takes the following path through the system, from submission to the label a reader sees:

1. **`POST /submit`** — The creator sends a JSON body containing `text` and `creator_id`. This is the entry point to the entire pipeline.

2. **Input Validation** — Flask checks that `text` is present and non-empty. If validation fails, the request is rejected immediately with a `400` error before any signal processing occurs.

3. **Rate Limiter** — Flask-Limiter checks whether this client has exceeded the configured request limit. If the limit is exceeded, the request is rejected with a `429` response. No signal processing, no log entry.

4. **Signal 1 — Groq LLM (`llama-3.3-70b-versatile`)** — The raw text is sent to the Groq API with a structured prompt asking the model to assess whether the text reads as AI-generated or human-written. The model returns a `signal_1_score` between 0.0 and 1.0, where 1.0 means the model is highly confident the text is AI-generated.

5. **Signal 2 — Stylometric Heuristics (pure Python)** — The same raw text is analyzed locally. The function computes sentence-length variance, type-token ratio (unique words ÷ total words), and punctuation density, then combines these into a single `signal_2_score` between 0.0 and 1.0, where 1.0 means the text is statistically very uniform (AI-like).

6. **Confidence Scorer** — Both signal scores are combined (weighted average) into a single `confidence` value (0.0–1.0). Confidence here measures *how certain the system is of its verdict*, not which direction the verdict goes. Confidence is highest when both signals strongly agree; lowest when they conflict or both sit near the midpoint.

7. **Label Generator** — The `attribution` direction (`likely_ai`, `likely_human`, or `uncertain`) is determined by where the signals point and how much they agree. The label generator then maps `attribution` + `confidence` to one of three transparency label texts for display to the reader.

8. **Audit Log** — Before returning the response, a structured entry is written to the audit log with the following fields:

   | Field | Description |
   |---|---|
   | `content_id` | UUID generated at submission time |
   | `creator_id` | Passed in from the request |
   | `timestamp` | ISO 8601 UTC |
   | `signal_1_score` | Raw Groq LLM score (0.0–1.0) |
   | `signal_2_score` | Raw stylometric score (0.0–1.0) |
   | `attribution` | `likely_ai` / `likely_human` / `uncertain` |
   | `confidence` | Combined certainty score (0.0–1.0) |
   | `label` | The exact label text shown to the reader |
   | `status` | `classified` on initial decision; `under_review` after appeal |

9. **JSON Response** — Flask returns the structured response to the caller: `content_id`, `attribution`, `confidence`, and `label`.

---

## Artifact 2 — Two Detection Signals

### Signal 1 — Groq LLM Classifier

**(a) What it measures:**
Semantic and stylistic coherence holistically — does this text *feel* AI-generated? The model picks up on balanced sentence structure, hedging language ("it is important to note that…"), lack of genuine personal voice, and overly consistent tone throughout the piece.

**(b) Why this property differs between human and AI writing:**
AI models are trained to produce safe, well-structured, consistently-toned output. Human writing carries idiosyncratic word choices, emotional inconsistency, tangents, and an uneven rhythm that reflects a real person's thought process. An LLM evaluator can detect the absence of those irregularities even when it cannot articulate exactly why.

**(c) Blind spots:**
- A skilled human deliberately writing in a formal, structured style will fool it.
- A heavily edited AI draft that had the rough edges added back in will score low.
- It is expensive per-call and adds latency to every request.
- It captures nothing about the statistical *structure* of the text — only its meaning and style holistically.

---

### Signal 2 — Stylometric Heuristics (Pure Python)

**(a) What it measures:**
Statistical structural patterns computed directly from the text: sentence-length variance (how much sentence lengths vary from the mean), type-token ratio (unique words ÷ total words, a measure of vocabulary diversity), and punctuation density (punctuation marks ÷ total characters).

**(b) Why these properties differ between human and AI writing:**
AI text is statistically uniform — sentence lengths cluster around a predictable range, vocabulary spread is consistent, and punctuation follows standard patterns. Human writing is messier: sentence lengths jump around, vocabulary repeats or surprises unpredictably, and punctuation reflects personal style (em dashes, ellipses, missing commas). High uniformity → high AI-likeness score.

**(c) Blind spots:**
- Formal or academic human writing (legal documents, scientific papers) is structurally uniform and looks AI-like to this signal.
- Short texts (fewer than ~5 sentences) do not provide enough data for reliable statistics — variance estimates are noisy.
- It is completely blind to meaning — it cannot tell the difference between a bureaucratic human memo and an AI essay if their statistical profiles match.

---

**Why these two signals are distinct:** Signal 1 is **semantic** (meaning and style) and Signal 2 is **structural** (statistical patterns). Each covers the other's primary blind spot — a formal human text that fools Signal 2 on structure will likely not fool Signal 1 on voice, and vice versa. This independence is the core justification for multi-signal detection.

---

## Artifact 3 — False-Positive Trace

**Scenario:** A non-native English speaker submits a formal poem with simple, repetitive vocabulary and uniform sentence structure.

- **Signal 2** scores it high on AI-likeness: sentence lengths are uniform, vocabulary diversity (type-token ratio) is low due to repetition, and the structure is tidy. `signal_2_score ≈ 0.72`.
- **Signal 1** scores it moderately: the phrasing is formal and lacks conversational irregularity, but the LLM may detect something personally expressive in the imagery. `signal_1_score ≈ 0.58`.
- The signals partially conflict and both sit in the mid-range. The confidence scorer recognizes this disagreement and returns a low confidence value. The direction is not clearly AI or clearly human.
- Result: `attribution: uncertain`, `confidence ≈ 0.55`. The system does **not** return `likely_ai`.
- The label shown to the reader is the **uncertain variant** — cautious, non-accusatory language that communicates "our system is not sure" rather than "this looks AI-generated."
- The creator sees the label, disagrees, and sends `POST /appeal` with their `content_id` and a written explanation ("I am a non-native English speaker and my writing style may appear more formal than typical").
- The system flips `status` to `under_review`, logs the appeal alongside the original classification entry, and returns a confirmation.

**Design decision this forces for Milestone 2:** The "uncertain" verdict must be the *default landing zone* for ambiguous cases — not a rare edge case. This means the uncertain band must be **wide** (e.g. roughly 0.35–0.70 on the confidence axis), so that the system requires genuinely strong, consistent signal agreement before committing to a `likely_ai` verdict. A narrow uncertain band (e.g. 0.45–0.55) would push borderline cases into accusations, which is worse than a false negative on a writing platform. The exact threshold numbers are an open decision for Milestone 2; this trace is the reason the band must lean wide and cautious.

---

## Artifact 4 — API Surface

| Endpoint | Method | Accepts | Returns |
|----------|--------|---------|---------|
| `/submit` | POST | `text`, `creator_id` | `content_id`, `attribution` (`likely_ai` \| `likely_human` \| `uncertain`), `confidence` (float 0.0–1.0), `label` (string) |
| `/appeal` | POST | `content_id`, `creator_reasoning` | `content_id`, `status: "under_review"`, `message` (confirmation string) |
| `/log` | GET | nothing (optional `?limit=N`) | `{ "entries": [...] }` — array of structured audit log entries |

**Note:** `content_id` is the binding thread of the entire system. It is generated by `/submit`, returned in the response, required by `/appeal` to locate the original decision, and recorded in every audit log entry. Without it, the appeal workflow has nothing to look up and the audit log cannot link decisions to appeals.

---

## Architecture

### Submission Flow

```mermaid
flowchart TD
    A([POST /submit\ntext, creator_id]) -->|text, creator_id| B[Input Validation]
    B -->|400 Bad Request| ERR1([Error Response])
    B -->|raw text| C[Rate Limiter]
    C -->|429 Too Many Requests| ERR2([Error Response])
    C -->|raw text| D[Signal 1: Groq LLM]
    C -->|raw text| E[Signal 2: Stylometrics]
    D -->|signal_1_score 0-1| F[Confidence Scorer]
    E -->|signal_2_score 0-1| F
    F -->|attribution + confidence| G[Label Generator]
    G -->|label text| H[Audit Log]
    F -->|attribution + confidence| H
    D -->|signal_1_score| H
    E -->|signal_2_score| H
    H -->|content_id, attribution,\nconfidence, label| I([JSON Response])
```

---

### Appeal Flow

```mermaid
flowchart TD
    A([POST /appeal\ncontent_id, creator_reasoning]) -->|content_id| B[Lookup content_id\nin Audit Log]
    B -->|not found| ERR([404 Error Response])
    B -->|existing record| C[Update Status\nto under_review]
    C -->|updated record + reasoning| D[Audit Log\nAppend Appeal Entry]
    D -->|content_id, status,\nconfirmation message| E([JSON Response])
```

---

## Milestone 1 Checkpoint

- [x] Full path of a submitted piece of text named end-to-end, every component listed in order
- [x] Two detection signals defined, each with what it measures, why it differs, and its blind spot
- [x] Three API endpoints listed with inputs and outputs
- [x] Submission flow diagrammed with labeled arrows
- [x] Appeal flow diagrammed with labeled arrows
