# Model card — ReconProof match scorer

## What it is

A calibrated binary classifier that scores whether two financial records refer to
the same transaction, used only for pairs that deterministic identifier matching
and exact subset attribution could not resolve.

Reproduce every number here with `make demo`.

| | |
|---|---|
| Task | Binary classification over candidate record pairs |
| Model | Logistic regression, standardized features, balanced class weights |
| Features | 22 interpretable features (see below) |
| Output | Probability, plus a risk estimate read off the calibration curve |
| Governs | Whether a link may be posted without a human |

## Why logistic regression

Gradient boosting scored comparably on this data and was not chosen. The
coefficients of a linear model map directly onto the interpretable features,
which keeps the counterfactual explanations shown to reviewers faithful to what
the model actually did. An explanation that does not describe the real decision
is worse than no explanation in a finance workflow.

The evaluation reports the simpler baselines alongside the model, so this choice
is defended with numbers rather than asserted.

## Training protocol

Three datasets from three seeds, each with exactly one job.

| Dataset | Seed | Size | Distribution | Purpose |
|---|---|---|---|---|
| Fit | 1001 | 14,000 orders | **Augmented** | Fit coefficients |
| Calibration | 3003 | 45,000 orders | Realistic | Choose thresholds |
| Held-out | 2002 | 1,000 orders | Realistic | Report performance |

The fitting set deliberately over-samples truncated references, typos and
same-amount decoys, because the model needs to see hard cases often enough to
learn from them.

**Calibration deliberately does not use that distribution.** A risk bound
measured on adversarially hard data does not transfer to production: it reads
pessimistic in a way that silently disables automation, and would read optimistic
if the augmentation were ever milder than reality. This was a genuine error
during development — thresholds were initially calibrated on the augmented set,
which plateaued the bound at 0.9876 and looked like a sample-size problem when it
was a methodology problem.

Links resolved deterministically are excluded from both fitting and calibration.
The model never scores them, so including them would describe a population the
threshold does not govern.

## Selective risk control

A probability of `0.99` says nothing about how often the *accepted set as a whole*
is wrong. So a threshold is selected on held-out calibration data as the lowest
value whose one-sided Wilson lower bound on precision still clears the target.
This is conformal risk control in the Learn-then-Test sense: a parameter chosen
so a bound on the risk of the selected set holds with high probability.

Using the bound rather than the empirical ratio matters because `5/5` and
`500/500` are both 100% empirically while carrying very different evidence. The
consequence is intentional: **thin evidence produces a conservative threshold**,
and a relation whose bound cannot reach the target keeps automation off rather
than running at an unproven level.

### Why per relation

A single global threshold has to satisfy the hardest relation. With one, the whole
system disabled automation; the easy relation was penalised for the hard one's
ambiguity.

| Relation | Threshold | Proven bound | Calibration rows |
|---|---|---|---|
| `order_to_payment` | 0.005 | ≥ 99.84% | 29,834 |
| `settlement_to_bank_credit` | 0.899 | ≥ 99.02% | 764 |
| `refund_to_settlement` | — | resolved by subset-sum instead | — |

The low `order_to_payment` threshold is not a weak gate. The relation separates
perfectly on this data, so every candidate above the floor is correct and the
bound is set by sample size rather than by errors.

## Held-out performance

Seed 2002, 1,000 orders, 3,110 records, realistic corruption rates.

| Metric | Value |
|---|---|
| Precision | **1.0000** |
| Recall | **0.9918** |
| False positives | **0** |
| Candidates scored | 141 |
| Accepted | 121 |

### Against baselines

| Approach | Precision | Recall | F1 |
|---|---|---|---|
| Exact reference only | 0.500 | 0.008 | 0.015 |
| Amount + date window | 0.865 | 0.910 | 0.887 |
| Hand-weighted heuristic | 0.992 | 0.910 | 0.949 |
| **Calibrated model** | **1.000** | **0.992** | **0.996** |

The naive amount-and-window rule is the honest comparison, and it is where the
model earns its place: 86.5% precision means roughly one wrong link in seven,
which in a ledger is not acceptable at any volume.

## Features

Structural and interpretable. Every one can be stated as a sentence a reviewer
would accept as a reason.

**Amount** — exact match, log difference, relative difference, 100x-ratio flag,
currency match

**Time** — signed day delta, absolute delta, deviation from the relation's
expected lag, within-window flag

**Reference** — exact match, containment, trailing-digit match, string similarity,
Jaro-Winkler

**Text** — description similarity, counterparty similarity

**Consistency** — payment-method compatibility, fee-rate plausibility

**Situational** — competing candidate count, sole-candidate flag, amount rank

Two deserve note. `competing_candidates` describes the situation rather than the
pair: a link that looks perfect alone is much weaker when three others look
equally perfect, and a model that cannot see this cannot learn to abstain.
`reference_containment` catches the common real case where a bank embeds a
shortened UTR inside free-text narration, so containment rather than equality is
the only surviving join key.

A `semantic_similarity` slot exists for sentence-transformer embeddings, disabled
by default. It is not needed to reach the reported numbers and adds a large
dependency, so it is opt-in.

## Limitations

- **Synthetic data only.** No real merchant transactions were used. The generator
  models the failure modes described in Razorpay's public documentation, but a
  real deployment would encounter formats it does not produce.
- **Two relations calibrated.** Others are resolved deterministically or routed
  to review on this data. A production deployment would need calibration
  evidence for each relation it intends to automate.
- **Refund attribution is capped.** Subset enumeration is abandoned above 24
  candidates and reported as unresolved. A guess would be worse than an explicit
  "too ambiguous to decide".
- **Single currency in practice.** Cross-currency records are detected and
  rejected rather than converted; no FX handling exists.
- **Calibration assumes stability.** The bound holds for the distribution it was
  measured on. Drift detection tightens automation when that assumption weakens,
  but it cannot re-establish the guarantee on its own.
- **Precision is prioritised over recall by design.** In reconciliation, ten
  withheld records cost review time; one wrong automatic match corrupts a
  reported figure. The thresholds reflect that asymmetry and would be wrong for a
  domain with the opposite cost structure.

## Language models

No language model participates in matching, arithmetic or any acceptance
decision. Their only roles are proposing which evidence to gather, proposing a
resolution for a human to approve, and phrasing an explanation — and every
proposal passes the deterministic verifier before it can matter.

The default provider uses no model at all. Ollama is used when reachable and an
Anthropic key enables a hosted provider; all three implement the same interface
and pass the same verifier, so the set of things the system will accept does not
change with the model.

## Monitoring

Population stability index across payment amount, fee rate, bank narration length
and reference length. Above the threshold, the risk budget **tightens**. It can
never loosen: a detector able to increase automation would be a mechanism for
silently voiding the guarantee it exists to protect.

## Retraining

Human resolutions become labelled examples, including the negatives implied by
choosing one candidate over its competitors — discarding those would train the
model to repeat the mistake it was corrected for.

Labels are stored, never applied. Retraining produces a challenger that must
clear the policy's promotion gates (precision lower bound, no regression against
the incumbent) and then be promoted by a named human. Automatic promotion on
metrics alone would let the system change the rules governing its own automation
without anyone deciding to allow it.
