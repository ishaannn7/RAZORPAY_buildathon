# Five-minute pitch

Record this as the Buildathon video. Speak to the product, not the stack.

## 0:00–0:40 — The problem

A merchant’s books are seven files that should describe the same money and
don’t. Orders, Razorpay payments, refunds, fees, settlements, webhooks, bank
credits. References vanish. Banks truncate them. Settlements net hundreds of
payments. Refunds land in a later window. A rupees column is occasionally read
as paise.

Closing the books is not “match similar rows.” It is deciding which records
correspond, and being able to defend each decision afterwards.

## 0:40–1:30 — What ReconProof refuses to do

It will not auto-post a match because a model said 0.99. A probability is not a
licence to write a ledger.

Automation is allowed only when three things are true at once:

1. The evidence is strong enough.
2. The accounting invariants hold in integer paise.
3. A proven precision bound for that *relation* still clears 99%.

Anything else goes to the exception queue, and the unexplained rupees in the
batch must equal the queue. If they don’t, the run is wrong.

## 1:30–3:00 — Demo (screen)

1. Dashboard of the seeded hold-out batch. Point at automatic match rate,
   settlement value traced, unexplained rupees, balanced settlements.
2. Open an exception. Show evidence, counterfactuals, blocking invariants.
3. Run investigation. Show the seven-phase trace, denied tools if any, verifier.
4. Approve — or override. Say why override is a separate button: it is how we
   measure that the agent was wrong.
5. Model & policy. Show per-relation thresholds and the Wilson bound. Show that
   promoting a model still needs a named human.
6. Optional: ingest a CSV. A structural fault rejects the file, not half of it.

## 3:00–4:20 — Where AI is used, and where it is not

| Layer | AI? | Why |
|---|---|---|
| Identifiers | No | An id is proof |
| Refund attribution | No | The total is exact; subset-sum |
| Ambiguous pairs | Calibrated logistic | Uncertain, but explanations must be faithful |
| Acceptance | Policy over a bound | The model does not set its own tolerance |
| Ledger arithmetic | No | Never a model’s job |
| Investigation | Bounded agent | Unstructured judgement over gathered evidence |
| Verification | No | The agent proposes; this decides |

The default investigation provider is a rule engine. Correctness does not depend
on a language model being present.

## 4:20–5:00 — What we would not ship

We would not put an LLM on the posting path. We would not calibrate on the same
distribution we fit on. We would not let drift loosen a threshold. We would not
call this a merchant result — every number is seeded synthetic data, reproduced
by `make demo`.

Close: ReconProof automates only what it can prove, and accounts for every rupee
it cannot.
