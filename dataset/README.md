# Demo dataset

Nine synthetic legal documents for demoing Legal Helper. Every party, person, and clause here is
invented — no real company text, no copied templates. Each document has known, deliberate problems
so a live review produces predictable findings.

Regenerate any of them with:

```bash
cd backend && .venv/bin/python ../dataset/generators/ndas.py            # and commercial.py, employment_and_data.py
```

## What each document demonstrates

| File | Type | Expected tier | Planted issues |
|---|---|---|---|
| `nda_mutual_balanced.docx` | Mutual NDA | 🟢 green | None — the "this one's fine" baseline. Use it first to show the tool doesn't invent problems. |
| `nda_vendor_evaluation_moderate.docx` | Mutual NDA | 🟡 yellow | 7-year term; broad residuals clause; no data-protection clause; one-sided assignment right |
| `nda_oneway_receiving_party_hostile.docx` | One-way NDA | 🔴 red | Perpetual confidentiality over all information; **no carve-outs at all**; 24-month employee + customer non-solicit; one-way injunctive relief and fee-shifting; no liability cap |
| `msa_professional_services.docx` | MSA (supplier side) | 🔴 red | Indemnity covering the customer's own negligence; IP assignment sweeping in the supplier's background IP and reusable tools; 90-day payment with unilateral set-off; 5-day termination for convenience with no payment for work in progress |
| `saas_subscription_agreement.docx` | SaaS terms (customer side) | 🔴 red | Auto-renewal with 90-day notice window; uncapped fee increases; liability capped at one month's fees with data loss excluded; no sub-processor notice or breach deadline; unilateral changes to the terms |
| `reseller_distribution_agreement.docx` | Distribution | 🟡 yellow | Exclusivity binding only the reseller; minimum commitments with no relief even for supplier non-delivery; asymmetric assignment; **no force majeure clause** |
| `employment_agreement_senior_engineer.docx` | Employment (employee side) | 🔴 red | 24-month worldwide non-compete, uncompensated; IP assignment covering personal-time and unrelated inventions with no prior-inventions carve-out; employer terminates with no notice while employee owes 90 days; perpetual confidentiality |
| `data_processing_agreement.docx` | DPA | 🟡/🔴 | Sub-processors appointed at will; no breach-notification deadline; no audit right; deletion "at the processor's discretion"; transfers anywhere with no mechanism |
| `consultancy_agreement_short.docx` | Consultancy | coverage demo | Deliberately sparse (~200 words). Its point is **absence**: no limitation of liability, no governing law, no IP ownership, vague payment terms. Use it to show the coverage agent's missing-required-clause detection. |

## Suggested demo order

1. **`nda_mutual_balanced.docx`** — establishes trust: few or no findings on a clean document.
2. **`nda_oneway_receiving_party_hostile.docx`** — the contrast: red tier, high-severity findings, obvious quotable spans. Good for showing Apply-all as tracked changes.
3. **`consultancy_agreement_short.docx`** — different failure mode: what's *missing* rather than what's wrong.
4. **`saas_subscription_agreement.docx`** or **`msa_professional_services.docx`** — a realistic full-length commercial review; good for Deep mode and the per-agent cost breakdown.

`samples/` (separate folder) holds the three fixtures the test suite uses — leave those alone; this
folder is the one to demo from.
