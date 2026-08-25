# Buildathon submission

Track: **AI Finance Controller** (Razorpay AI Buildathon).
Product: **ReconProof**.
Deadline: confirm on the official form (publicly stated **5 Sep 2026**).

## What to submit

1. **Public repository** — this repo. Default branch, `README.md` at the root.
2. **Architecture** — `docs/architecture.md` plus the in-app `/architecture` page.
3. **5-minute pitch video** — follow `docs/pitch.md`. Show the seeded demo, one
   exception investigation, and the model/policy page.

## What the committee said it is scoring

- Problem taste
- Build quality
- AI judgement, including where **not** to use AI
- Failure recovery

Motto: students who **build** with AI, not talk about it.

## How a reviewer runs it

```bash
make setup
make demo    # ~60s, fully seeded
make api     # http://127.0.0.1:8817
make web     # http://127.0.0.1:43917
make test
```

No Docker, no Postgres, no API key, no GPU.

## Claims that must still be true after `make demo`

Held-out seed is documented in the README and in
`.reconproof/artifacts/evaluation_report.json`.

- Match precision 1.0000 (0 false positives)
- Unexplained value fully represented in the exception queue
- Agent cannot post a match; promotion requires a named human

If any of those regress, do not submit the build.

## Honest limits

- Data is synthetic. Say so in the video.
- SQLite is the default; Postgres is a URL change.
- The language-model provider is optional. Demo with the deterministic provider.
- Semantic embeddings are off unless explicitly enabled.
