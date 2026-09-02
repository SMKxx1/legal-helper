"""Synthetic demo data (plan §4.6): users, ~140 reviews, and their ``llm_calls`` rows.

Run with ``python -m app.seed_demo`` (``--reset`` truncates ``reviews``/``llm_calls`` and reseeds;
also runs at boot when ``SEED_DEMO_DATA=true`` **and** the ``users`` table is empty — see
``main.create_app``'s lifespan). Fixed RNG seed (``random.Random(2026)``) so every deployment's
synthetic history looks the same.

Idempotent by construction, not by row-level dedup: :func:`seed_users` skips any username already
present, and :func:`seed_reviews` is a no-op whenever the ``reviews`` table is already non-empty —
so running the whole module twice creates nothing new the second time (see
``tests/test_seed_demo.py::test_seed_is_idempotent``, the one correctness risk here).

Every seeded user shares one password, ``DEMO_USER_PASSWORD`` — and carries no OpenRouter key.
Only the presenter's own account gets a real key, entered live in the add-in.

Every seeded review reuses the REAL persistence path (``reviews_repo.create_review`` /
``complete_review`` / ``fail_review``) with a hand-built ``ReviewResult`` standing in for what the
agent pipeline would have produced — so the JSON shape History renders is guaranteed identical to
a real review's, with zero duplicated serialization logic. Every seeded finding's ``span`` is
plausible-looking placeholder legalese (never excerpted from a real document) and
``span_faithful`` is always ``False``, so History → Open can never "Apply" a seeded finding to
whatever document happens to be open in Word.
"""

from __future__ import annotations

import argparse
import random
from datetime import UTC, datetime, timedelta, timezone
from typing import TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from .agents.orchestrator import CoverageReport, Finding, ReviewResult
from .ai.ledger import LlmCallRecord
from .api import reviews_repo
from .auth.security import hash_password
from .config import settings
from .db import SessionLocal, init_db
from .models import LlmCall, Review, User

#: (username, display_name, role) — fixed order/spelling so every deployment looks the same.
DEMO_USERS: list[tuple[str, str, str]] = [
    ("admin", "Admin", "admin"),
    ("alice.tan", "Alice Tan", "user"),
    ("ben.lim", "Ben Lim", "user"),
    ("chloe.ng", "Chloe Ng", "user"),
    ("dev.raj", "Dev Raj", "user"),
    ("emma.koh", "Emma Koh", "user"),
    ("farid.hassan", "Farid Hassan", "user"),
    ("grace.lee", "Grace Lee", "user"),
]


def seed_users(db: DbSession) -> int:
    """Insert any :data:`DEMO_USERS` row not already present. Returns the number created."""
    password_hash = hash_password(settings.demo_user_password)
    existing = {row[0] for row in db.execute(select(User.username)).all()}
    created = 0
    for username, display_name, role in DEMO_USERS:
        if username in existing:
            continue
        db.add(
            User(
                username=username,
                display_name=display_name,
                role=role,
                password_hash=password_hash,
            )
        )
        created += 1
    db.commit()
    return created


# --------------------------------------------------------------------------------------------- #
# Synthetic review history (plan §4.6)
# --------------------------------------------------------------------------------------------- #

_RNG_SEED = 2026
_REVIEW_COUNT = 140
_FAILED_REVIEW_COUNT = 3
_SGT = timezone(
    timedelta(hours=8)
)  # Singapore time — no zoneinfo needed for a fixed offset
_PLAYBOOK_VERSION = "lh-1"

#: username -> relative activity weight. Two heavy (alice.tan, ben.lim), two light
#: (farid.hassan, grace.lee), the rest moderate — "per-user activity skewed" (plan §4.6).
_ACTIVITY_WEIGHTS: dict[str, float] = {
    "admin": 1.0,
    "alice.tan": 3.0,
    "ben.lim": 3.0,
    "chloe.ng": 1.5,
    "dev.raj": 1.5,
    "emma.koh": 1.5,
    "farid.hassan": 0.5,
    "grace.lee": 0.5,
}

#: (doc_type key, weight) — plan §4.6: "NDA 35%, MSA 20%, SaaS subscription 15%, employment 10%,
#: lease 10%, DPA 10%".
_DOC_TYPE_WEIGHTS: list[tuple[str, float]] = [
    ("nda", 0.35),
    ("msa", 0.20),
    ("saas_subscription", 0.15),
    ("employment", 0.10),
    ("lease", 0.10),
    ("dpa", 0.10),
]

#: doc_type key -> (classifier-style doc_type string, our_side default, filename stems).
_DOC_TYPE_INFO: dict[str, dict] = {
    "nda": {
        "doc_type": "mutual_nda",
        "our_side": "the Receiving Party",
        "stems": ["{co}_NDA_{ym}", "{co}_Mutual_NDA_v{n}"],
    },
    "msa": {
        "doc_type": "master_services_agreement",
        "our_side": "the Customer",
        "stems": ["{co}_MSA_v{n}", "{co}_MSA_{ym}"],
    },
    "saas_subscription": {
        "doc_type": "saas_subscription_agreement",
        "our_side": "the Customer",
        "stems": ["{co}_SaaS_Subscription_v{n}", "{co}_Subscription_Agreement_{ym}"],
    },
    "employment": {
        "doc_type": "employment_agreement",
        "our_side": "the Employer",
        "stems": ["{co}_Employment_Agreement_{ym}", "{co}_Offer_Letter_v{n}"],
    },
    "lease": {
        "doc_type": "commercial_lease",
        "our_side": "the Tenant",
        "stems": ["{co}_Lease_{ym}", "{co}_Office_Lease_v{n}"],
    },
    "dpa": {
        "doc_type": "data_processing_agreement",
        "our_side": "the Data Controller",
        "stems": ["{co}_DPA_v{n}", "{co}_Data_Processing_Addendum_{ym}"],
    },
}

_COMPANIES = [
    "Acme",
    "Northwind",
    "Globex",
    "Initech",
    "Umbrella",
    "Hooli",
    "Soylent",
    "Stark",
    "Wayne",
    "Wonka",
    "Cyberdyne",
    "Vandelay",
    "Contoso",
    "Fabrikam",
    "Tyrell",
    "Massive Dynamic",
]

#: (risk_tier, weight) — plan §4.6: "35/45/20 (green/yellow/red)".
_RISK_TIER_WEIGHTS: list[tuple[str, float]] = [
    ("green", 0.35),
    ("yellow", 0.45),
    ("red", 0.20),
]
#: (mode, weight) — plan §4.6: "65% quick / 35% deep".
_MODE_WEIGHTS: list[tuple[str, float]] = [("quick", 0.65), ("deep", 0.35)]

#: Realistic provider error codes for the handful of `failed` rows (plan §4.6; codes taken from
#: `ai.gateway.error_code_for`).
_FAILURE_CODES = ["rate_limited", "insufficient_credits", "timeout"]

_MODEL_CLASSIFIER = "anthropic/claude-haiku-4-5"
_MODEL_QUICK = "anthropic/claude-sonnet-4-6"
_MODEL_DEEP = "anthropic/claude-opus-4-8"

_REQUIRED_CLAUSES = [
    "confidentiality",
    "term_and_termination",
    "limitation_of_liability",
    "governing_law_and_disputes",
]
_ALL_CLAUSE_TYPES = _REQUIRED_CLAUSES + [
    "indemnification",
    "intellectual_property",
    "payment_terms",
    "assignment",
    "non_solicit_non_compete",
    "data_protection",
    "warranties",
    "force_majeure",
]

#: ~25 hand-written findings, keyed by clause type (plan §4.6). Placeholder legalese only — never
#: excerpted from a real document. `span_faithful` is forced to False when these are turned into
#: `Finding`s, so nothing here can ever anchor a tracked change in an unrelated open document.
_FINDING_POOL: list[dict] = [
    {
        "clause_type": "confidentiality",
        "clause_heading": "4. Confidentiality",
        "severity": "medium",
        "title": "Confidentiality survives termination indefinitely",
        "rationale": "The obligation covers all information, not just trade secrets, with no time limit — broader than the standard position.",
        "span": "The obligations of confidentiality under this Section shall survive termination of this Agreement indefinitely.",
        "suggested_language": "The obligations of confidentiality under this Section shall survive termination of this Agreement for five (5) years, except that obligations relating to trade secrets shall survive for as long as the information remains a trade secret.",
        "change_type": "modify",
    },
    {
        "clause_type": "confidentiality",
        "clause_heading": "4. Confidentiality",
        "severity": "high",
        "title": "No standard carve-outs from confidentiality",
        "rationale": "The definition of confidential information has no carve-out for information that is already public, already known, or independently developed.",
        "span": '"Confidential Information" means any information disclosed by either party to the other, in any form.',
        "suggested_language": '"Confidential Information" excludes information that (a) is or becomes public through no fault of the receiving party, (b) was already known to the receiving party without an obligation of confidentiality, (c) is independently developed without use of the disclosing party\'s Confidential Information, or (d) is rightfully received from a third party.',
        "change_type": "modify",
    },
    {
        "clause_type": "term_and_termination",
        "clause_heading": "2. Term and Termination",
        "severity": "medium",
        "title": "No termination-for-convenience right",
        "rationale": "Only the counterparty may terminate for cause; our side has no right to exit the agreement on notice.",
        "span": "This Agreement may be terminated by [Counterparty] upon thirty (30) days' written notice.",
        "suggested_language": "This Agreement may be terminated by either party upon thirty (30) days' written notice.",
        "change_type": "modify",
    },
    {
        "clause_type": "term_and_termination",
        "clause_heading": "2. Term and Termination",
        "severity": "high",
        "title": "Automatic renewal with a narrow opt-out window",
        "rationale": "The agreement auto-renews annually unless notice is given more than 90 days before the renewal date — an easy trap to miss.",
        "span": "This Agreement shall automatically renew for successive one-year terms unless either party provides written notice of non-renewal at least ninety (90) days prior to the end of the then-current term.",
        "suggested_language": "This Agreement shall automatically renew for successive one-year terms unless either party provides written notice of non-renewal at least thirty (30) days prior to the end of the then-current term.",
        "change_type": "modify",
    },
    {
        "clause_type": "limitation_of_liability",
        "clause_heading": "9. Limitation of Liability",
        "severity": "high",
        "title": "Liability is uncapped on our side",
        "rationale": "The clause caps the counterparty's liability but places no cap on ours — a one-sided exposure that leaves us bearing unlimited risk.",
        "span": "In no event shall [Counterparty]'s aggregate liability exceed the fees paid in the twelve (12) months preceding the claim.",
        "suggested_language": "In no event shall either party's aggregate liability exceed the fees paid in the twelve (12) months preceding the claim.",
        "change_type": "modify",
    },
    {
        "clause_type": "limitation_of_liability",
        "clause_heading": "9. Limitation of Liability",
        "severity": "medium",
        "title": "No exclusion of consequential damages",
        "rationale": "The clause omits the standard mutual exclusion of indirect and consequential damages, leaving open-ended downstream exposure.",
        "span": "Nothing in this Section limits either party's liability for direct damages arising from a breach of this Agreement.",
        "suggested_language": "Neither party shall be liable to the other for any indirect, incidental, special, or consequential damages, including loss of profits or revenue, however arising.",
        "change_type": "modify",
    },
    {
        "clause_type": "indemnification",
        "clause_heading": "10. Indemnification",
        "severity": "high",
        "title": "Indemnification obligations run one way",
        "rationale": "Only our side indemnifies the counterparty; there is no reciprocal obligation for claims arising from the counterparty's own breach or negligence.",
        "span": "[Our Company] shall indemnify and hold harmless [Counterparty] from any claim arising out of [Our Company]'s performance of this Agreement.",
        "suggested_language": "Each party shall indemnify and hold harmless the other from any third-party claim arising out of the indemnifying party's breach of this Agreement, infringement of intellectual property rights, or negligence.",
        "change_type": "modify",
    },
    {
        "clause_type": "indemnification",
        "clause_heading": "10. Indemnification",
        "severity": "medium",
        "title": "Indemnification carved out of the liability cap",
        "rationale": "Indemnification obligations are excluded from the liability cap in Section 9, creating effectively uncapped exposure through this route.",
        "span": "The limitation of liability in Section 9 shall not apply to either party's obligations under this Section.",
        "suggested_language": "Indemnification obligations under this Section are subject to the liability cap set out in Section 9, except for claims arising from gross negligence or willful misconduct.",
        "change_type": "modify",
    },
    {
        "clause_type": "intellectual_property",
        "clause_heading": "7. Intellectual Property",
        "severity": "high",
        "title": "Broad, perpetual IP license granted to counterparty",
        "rationale": "The clause grants a perpetual, royalty-free license to our pre-existing IP well beyond what is needed to perform the agreement.",
        "span": "[Our Company] grants [Counterparty] a perpetual, irrevocable, royalty-free license to use any intellectual property provided under this Agreement for any purpose.",
        "suggested_language": "[Our Company] grants [Counterparty] a non-exclusive, royalty-free license to use the intellectual property provided under this Agreement solely as needed to perform the parties' obligations, for the term of this Agreement.",
        "change_type": "modify",
    },
    {
        "clause_type": "intellectual_property",
        "clause_heading": "7. Intellectual Property",
        "severity": "medium",
        "title": "Jointly developed IP ownership left undefined",
        "rationale": "The agreement contemplates joint development work but never addresses who owns the resulting IP.",
        "span": "The parties may collaborate on the development of new features and functionality under this Agreement.",
        "suggested_language": "Any intellectual property jointly developed by the parties under this Agreement shall be jointly owned, with each party free to exploit it subject to a duty to account for licensing revenue to the other.",
        "change_type": "modify",
    },
    {
        "clause_type": "governing_law_and_disputes",
        "clause_heading": "15. Governing Law",
        "severity": "medium",
        "title": "Exclusive jurisdiction impractical for our side",
        "rationale": "Disputes must be litigated exclusively in the counterparty's home jurisdiction, which is inconvenient and costly for our side to enforce.",
        "span": "The parties submit to the exclusive jurisdiction of the courts of [Counterparty's Home State].",
        "suggested_language": "The parties submit to the non-exclusive jurisdiction of the courts of a mutually agreed, neutral jurisdiction.",
        "change_type": "modify",
    },
    {
        "clause_type": "payment_terms",
        "clause_heading": "5. Payment Terms",
        "severity": "medium",
        "title": "Uncapped late-payment interest",
        "rationale": "The late-payment interest rate is not capped and compounds monthly, which can become punitive over a long dispute.",
        "span": "Overdue amounts shall accrue interest at 3% per month, compounding, until paid in full.",
        "suggested_language": "Overdue amounts shall accrue interest at the lesser of 1.5% per month or the maximum rate permitted by law, non-compounding.",
        "change_type": "modify",
    },
    {
        "clause_type": "payment_terms",
        "clause_heading": "5. Payment Terms",
        "severity": "low",
        "title": "No good-faith invoice dispute process",
        "rationale": "There is no defined process for disputing an invoice in good faith before late fees begin to accrue.",
        "span": "All invoices are due and payable within fifteen (15) days of receipt.",
        "suggested_language": "A party disputing an invoice in good faith shall notify the other within fifteen (15) days of receipt; late fees shall not accrue on the disputed portion pending resolution.",
        "change_type": "modify",
    },
    {
        "clause_type": "assignment",
        "clause_heading": "16. Assignment",
        "severity": "medium",
        "title": "Counterparty may freely assign without consent",
        "rationale": "The counterparty can assign the agreement to anyone, including a competitor, without our consent or even notice.",
        "span": "[Counterparty] may assign this Agreement at any time without the prior consent of [Our Company].",
        "suggested_language": "Neither party may assign this Agreement without the other party's prior written consent, not to be unreasonably withheld, except to an affiliate or in connection with a merger or sale of substantially all assets.",
        "change_type": "modify",
    },
    {
        "clause_type": "non_solicit_non_compete",
        "clause_heading": "17. Non-Solicitation",
        "severity": "medium",
        "title": "One-sided, 36-month non-solicit",
        "rationale": "The non-solicit binds only our side and runs for three years — well beyond the standard 12-month, mutual position.",
        "span": "[Our Company] shall not solicit for employment any employee of [Counterparty] for a period of thirty-six (36) months following termination.",
        "suggested_language": "Neither party shall solicit for employment any employee of the other party for a period of twelve (12) months following termination, excluding general job postings and unsolicited applications.",
        "change_type": "modify",
    },
    {
        "clause_type": "non_solicit_non_compete",
        "clause_heading": "17. Non-Solicitation",
        "severity": "high",
        "title": "Non-compete restricting our core business",
        "rationale": "A broad non-compete clause restricts our side from operating in an entire market segment, not just from soliciting the counterparty's staff.",
        "span": "[Our Company] shall not, during the term and for two years thereafter, engage in any business competitive with [Counterparty]'s business.",
        "suggested_language": "This Agreement shall not restrict either party's ordinary business activities; delete this non-compete provision in its entirety.",
        "change_type": "delete",
    },
    {
        "clause_type": "data_protection",
        "clause_heading": "12. Data Protection",
        "severity": "high",
        "title": "No breach-notification obligation",
        "rationale": "The agreement clearly involves processing personal data but includes no obligation to notify the other party of a security incident.",
        "span": "Each party shall comply with applicable data protection law in connection with this Agreement.",
        "suggested_language": "Each party shall comply with applicable data protection law and shall notify the other party without undue delay, and in any event within 72 hours, upon becoming aware of a personal data breach affecting the other party's data.",
        "change_type": "modify",
    },
    {
        "clause_type": "data_protection",
        "clause_heading": "12. Data Protection",
        "severity": "medium",
        "title": "No data processing addendum referenced",
        "rationale": "One party processes personal data on the other's behalf, but no DPA or equivalent terms govern purpose, security, or sub-processors.",
        "span": "[Counterparty] may process personal data as reasonably necessary to provide the services.",
        "suggested_language": "The parties shall enter into a data processing addendum governing the purpose, security measures, and sub-processor terms applicable to any personal data processed under this Agreement.",
        "change_type": "modify",
    },
    {
        "clause_type": "warranties",
        "clause_heading": "8. Warranties",
        "severity": "medium",
        "title": "Warranties run only from our side",
        "rationale": "Our side gives warranties of authority and non-conflict; the counterparty gives none, an imbalance from the standard mutual position.",
        "span": "[Our Company] represents and warrants that it has full authority to enter into this Agreement.",
        "suggested_language": "Each party represents and warrants that it has full authority to enter into this Agreement and that doing so does not conflict with any other obligation.",
        "change_type": "modify",
    },
    {
        "clause_type": "warranties",
        "clause_heading": "8. Warranties",
        "severity": "low",
        "title": "Open-ended fitness-for-purpose warranty",
        "rationale": "An unqualified warranty of fitness for a particular purpose is broader than the standard limited warranty position.",
        "span": "[Counterparty] warrants that the deliverables will be fit for [Our Company]'s intended purpose.",
        "suggested_language": "[Counterparty] warrants that the deliverables will materially conform to the specifications set out in this Agreement.",
        "change_type": "modify",
    },
    {
        "clause_type": "force_majeure",
        "clause_heading": "18. Force Majeure",
        "severity": "low",
        "title": "Force majeure excuses only the counterparty",
        "rationale": "The clause excuses the counterparty's non-performance but not ours — a one-sided position on an otherwise standard boilerplate clause.",
        "span": "[Counterparty] shall not be liable for any delay caused by events beyond its reasonable control.",
        "suggested_language": "Neither party shall be liable for any delay or failure to perform caused by events beyond its reasonable control, provided the affected party gives prompt notice.",
        "change_type": "modify",
    },
    {
        "clause_type": "force_majeure",
        "clause_heading": "18. Force Majeure",
        "severity": "medium",
        "title": "No termination right for prolonged force majeure",
        "rationale": "There is no fallback termination right if a force majeure event continues indefinitely, leaving both sides stuck.",
        "span": "A party affected by a force majeure event shall be excused from performance for the duration of the event.",
        "suggested_language": "A party affected by a force majeure event shall be excused from performance for the duration of the event; either party may terminate this Agreement on notice if the event continues for more than sixty (60) consecutive days.",
        "change_type": "modify",
    },
    {
        "clause_type": "payment_terms",
        "clause_heading": "5. Payment Terms",
        "severity": "high",
        "title": "Payment due on receipt, no dispute process at all",
        "rationale": "Invoices are due immediately on receipt with no grace period and no process to dispute a billing error in good faith.",
        "span": "All amounts invoiced are due and payable immediately upon receipt.",
        "suggested_language": "All amounts invoiced are due and payable within thirty (30) days of receipt, subject to a good-faith dispute process for any invoice believed to be in error.",
        "change_type": "modify",
    },
    {
        "clause_type": "intellectual_property",
        "clause_heading": "7. Intellectual Property",
        "severity": "low",
        "title": "Pre-existing IP ownership not expressly reserved",
        "rationale": "The agreement never expressly states that each party keeps ownership of IP it brings to the relationship — worth stating explicitly to avoid later disputes.",
        "span": "This Agreement does not address ownership of intellectual property existing prior to the Effective Date.",
        "suggested_language": "Each party retains all right, title, and interest in and to its intellectual property existing prior to the Effective Date; nothing in this Agreement transfers ownership of such pre-existing intellectual property.",
        "change_type": "modify",
    },
]


_T = TypeVar("_T")


def _weighted_choice(rng: random.Random, options: list[tuple[_T, float]]) -> _T:
    """Pick one value from ``[(value, weight), ...]``."""
    total = sum(w for _, w in options)
    r = rng.uniform(0, total)
    upto = 0.0
    for value, weight in options:
        upto += weight
        if r <= upto:
            return value
    return options[-1][0]


def _random_timestamp(rng: random.Random, now_utc: datetime) -> datetime:
    """A UTC timestamp within the last 60 days, weighted toward the last two weeks (a triangular
    distribution peaked at "today") and toward SGT weekday business hours (plan §4.6)."""
    now_sgt = now_utc.astimezone(_SGT)
    candidate_date = now_sgt.date()
    for _ in range(
        6
    ):  # a handful of redraws to bias away from weekends, not a hard filter
        day_offset = int(rng.triangular(0, 60, 0))
        candidate_date = (now_sgt - timedelta(days=day_offset)).date()
        if (
            candidate_date.weekday() < 5 or rng.random() < 0.2
        ):  # Mon-Fri, or 20% keep a weekend
            break
    hour = rng.randint(9, 18)
    local_dt = datetime(
        candidate_date.year,
        candidate_date.month,
        candidate_date.day,
        hour,
        rng.randint(0, 59),
        rng.randint(0, 59),
        tzinfo=_SGT,
    )
    return local_dt.astimezone(UTC)


def _findings_for_tier(rng: random.Random, tier: str) -> list[dict]:
    """Pick pool entries consistent with ``tier`` so a seeded review's risk badge matches what its
    findings would actually imply (green: only low-severity noise; yellow: at least one medium,
    no high; red: at least one high)."""
    low = [f for f in _FINDING_POOL if f["severity"] == "low"]
    medium = [f for f in _FINDING_POOL if f["severity"] == "medium"]
    high = [f for f in _FINDING_POOL if f["severity"] == "high"]
    if tier == "green":
        n = rng.randint(0, 2)
        return rng.sample(low, min(n, len(low)))
    if tier == "yellow":
        n = rng.randint(2, 4)
        must = rng.choice(medium)
        pool = [f for f in medium + low if f is not must]
        rest = rng.sample(pool, min(max(n - 1, 0), len(pool)))
        return [must, *rest]
    # red
    n = rng.randint(2, 5)
    must = rng.choice(high)
    pool = [f for f in high + medium if f is not must]
    rest = rng.sample(pool, min(max(n - 1, 0), len(pool)))
    return [must, *rest]


def _coverage_for(rng: random.Random, tier: str, mode: str) -> CoverageReport | None:
    """Deep-mode-only coverage report; occasionally supplies the "absent required clause" half of
    a red tier instead of (or alongside) a high-severity finding."""
    if mode != "deep":
        return None
    absent: list[dict] = []
    if tier == "red" and rng.random() < 0.35:
        clause = rng.choice(_REQUIRED_CLAUSES)
        absent = [
            {
                "clause_type": clause,
                "note": f"No {clause.replace('_', ' ')} clause found in the document.",
            }
        ]
    return CoverageReport(checked=list(_ALL_CLAUSE_TYPES), absent_required=absent)


def _cost_band(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 6)


def _build_call(
    rng: random.Random,
    agent: str,
    model: str,
    *,
    cost_lo: float,
    cost_hi: float,
    tokens_lo: int,
    tokens_hi: int,
    latency_lo: int,
    latency_hi: int,
) -> LlmCallRecord:
    prompt_tokens = rng.randint(tokens_lo, tokens_hi)
    completion_tokens = rng.randint(max(50, tokens_lo // 6), max(100, tokens_hi // 4))
    return LlmCallRecord(
        agent=agent,
        model=model,
        provider=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=0,
        cost_usd=_cost_band(rng, cost_lo, cost_hi),
        latency_ms=rng.randint(latency_lo, latency_hi),
        ok=True,
    )


def _calls_for(rng: random.Random, mode: str) -> list[LlmCallRecord]:
    """2 rows for a quick review (classifier + reviewer), 3 for deep (+ coverage) — plan §4.6's
    cost/token bands per agent."""
    calls = [
        _build_call(
            rng,
            "classifier",
            _MODEL_CLASSIFIER,
            cost_lo=0.001,
            cost_hi=0.006,
            tokens_lo=800,
            tokens_hi=2500,
            latency_lo=700,
            latency_hi=2500,
        )
    ]
    if mode == "deep":
        calls.append(
            _build_call(
                rng,
                "reviewer",
                _MODEL_DEEP,
                cost_lo=0.35,
                cost_hi=1.40,
                tokens_lo=6000,
                tokens_hi=20000,
                latency_lo=15000,
                latency_hi=90000,
            )
        )
        calls.append(
            _build_call(
                rng,
                "coverage",
                _MODEL_QUICK,
                cost_lo=0.03,
                cost_hi=0.10,
                tokens_lo=3000,
                tokens_hi=8000,
                latency_lo=3000,
                latency_hi=9000,
            )
        )
    else:
        calls.append(
            _build_call(
                rng,
                "reviewer",
                _MODEL_QUICK,
                cost_lo=0.03,
                cost_hi=0.12,
                tokens_lo=3000,
                tokens_hi=9000,
                latency_lo=3000,
                latency_hi=9000,
            )
        )
    return calls


def _filename_for(rng: random.Random, doc_key: str, created_at: datetime) -> str:
    info = _DOC_TYPE_INFO[doc_key]
    stem = rng.choice(info["stems"])
    company = rng.choice(_COMPANIES).replace(" ", "")
    return (
        stem.format(co=company, n=rng.randint(1, 6), ym=created_at.strftime("%Y-%m"))
        + ".docx"
    )


def _build_review_result(
    rng: random.Random, doc_key: str, mode: str, tier: str
) -> ReviewResult:
    info = _DOC_TYPE_INFO[doc_key]
    pool_findings = _findings_for_tier(rng, tier)
    findings = [
        Finding(
            id=i,
            clause_type=f["clause_type"],
            clause_heading=f["clause_heading"],
            severity=f["severity"],
            title=f["title"],
            rationale=f["rationale"],
            span=f["span"],
            span_faithful=False,  # never anchorable to a real open document (plan §4.6)
            suggested_language=f["suggested_language"],
            change_type=f["change_type"],
        )
        for i, f in enumerate(pool_findings, start=1)
    ]
    coverage = _coverage_for(rng, tier, mode)
    counts = {
        "high": sum(1 for f in findings if f.severity == "high"),
        "medium": sum(1 for f in findings if f.severity == "medium"),
        "low": sum(1 for f in findings if f.severity == "low"),
    }
    adherence_bands = {
        "green": (82.0, 97.0),
        "yellow": (55.0, 81.0),
        "red": (18.0, 54.0),
    }
    lo, hi = adherence_bands[tier]
    summary = (
        f"{info['doc_type'].replace('_', ' ').title()} reviewed against the standard playbook "
        f"from {info['our_side']}'s side — overall {tier} risk."
    )
    return ReviewResult(
        doc_type=info["doc_type"],
        our_side=info["our_side"],
        summary=summary,
        risk_tier=tier,
        adherence_score=round(rng.uniform(lo, hi), 1),
        counts=counts,
        findings=findings,
        coverage=coverage,
        warnings=[],
        calls=_calls_for(rng, mode),
        playbook_version=_PLAYBOOK_VERSION,
    )


def _backdate(
    db: DbSession, review: Review, created_at: datetime, duration_ms: int
) -> None:
    """Set ``finished_at``/each ``llm_calls.created_at`` to line up with the backdated
    ``created_at`` (already baked into ``review.result_json`` by the time this runs)."""
    review.finished_at = created_at + timedelta(milliseconds=duration_ms)
    for call in db.execute(
        select(LlmCall).where(LlmCall.review_id == review.id)
    ).scalars():
        call.created_at = created_at
    db.commit()


def seed_reviews(
    db: DbSession, rng: random.Random, now_utc: datetime | None = None
) -> int:
    """Seed ~140 reviews + their ``llm_calls`` (plan §4.6). A no-op (returns 0) whenever the
    ``reviews`` table is already non-empty — this table-emptiness check is the whole idempotency
    story: a second run of ``seed_demo`` creates nothing further."""
    if (db.execute(select(func.count(Review.id))).scalar() or 0) > 0:
        return 0

    now_utc = now_utc or datetime.now(UTC)
    users = {
        u.username: u
        for u in db.execute(
            select(User).where(User.username.in_([row[0] for row in DEMO_USERS]))
        ).scalars()
    }
    activity_weights = [
        (users[uname], weight)
        for uname, weight in _ACTIVITY_WEIGHTS.items()
        if uname in users
    ]
    if not activity_weights:
        return 0

    created = 0
    for i in range(_REVIEW_COUNT):
        user = _weighted_choice(rng, activity_weights)
        doc_key = _weighted_choice(rng, _DOC_TYPE_WEIGHTS)
        mode = _weighted_choice(rng, _MODE_WEIGHTS)
        created_at = _random_timestamp(rng, now_utc)
        filename = _filename_for(rng, doc_key, created_at)
        duration_ms = (
            rng.randint(70000, 220000) if mode == "deep" else rng.randint(12000, 42000)
        )

        review = reviews_repo.create_review(
            db, user, filename=filename, mode=mode, our_side="", status="running"
        )
        review.created_at = (
            created_at  # backdated BEFORE complete_review bakes it into result_json
        )
        db.flush()

        if i < _FAILED_REVIEW_COUNT:
            calls = _calls_for(rng, mode)[
                :1
            ]  # only the classifier call ran before it failed
            reviews_repo.fail_review(
                db,
                review,
                rng.choice(_FAILURE_CODES),
                duration_ms=duration_ms,
                calls=calls,
            )
        else:
            tier = _weighted_choice(rng, _RISK_TIER_WEIGHTS)
            result = _build_review_result(rng, doc_key, mode, tier)
            reviews_repo.complete_review(db, review, result, duration_ms=duration_ms)
        _backdate(db, review, created_at, duration_ms)
        created += 1
    return created


def run(*, reset: bool = False) -> None:
    init_db()
    with SessionLocal() as db:
        if reset:
            db.execute(delete(LlmCall))
            db.execute(delete(Review))
            db.commit()
        created_users = seed_users(db)
        print(
            f"seed_demo: {created_users} user(s) created, "
            f"{len(DEMO_USERS) - created_users} already present"
        )
        rng = random.Random(_RNG_SEED)
        created_reviews = seed_reviews(db, rng)
        if created_reviews:
            print(f"seed_demo: {created_reviews} review(s) + llm_calls seeded")
        else:
            print("seed_demo: reviews already seeded, skipped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Legal Helper demo data.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate reviews/llm_calls and reseed them.",
    )
    args = parser.parse_args()
    run(reset=args.reset)
