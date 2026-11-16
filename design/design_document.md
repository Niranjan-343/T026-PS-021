# Design Document — T026-PS-021
## Home Loan with Construction-Linked Disbursement Tranches and Pre-EMI

**Team T026 · Section CSDS-A**
- Nethraj T M (1AT24CD052)
- Niranjan (1AT24CD053)
- Palak Raj (1AT24CD054)

---

## 1. System Architecture

The engine is architected in two clean layers:
1. **Domain Models & Pure Rules (`src/models/`, `src/rules/`, `src/calculations/`)**:
   - `Borrower`, `Property`, `LoanAccount`, `DisburseRecord`, `PreEmiBill`, `EmiRecord`.
   - Business rules registry `BusinessRules` covering BR-01 through BR-26.
   - Deterministic Decimal arithmetic with `getcontext().prec = 28` and `ROUND_HALF_UP`.
2. **Stateful Event Processors (`src/engine/loan_engine.py`, `run_sample.py`)**:
   - Event-driven state machine handling the full loan lifecycle: Sanction → Tranche release → Pre-EMI accrual → Final tranche conversion → Rate resets → Prepayments → Closure.
   - Deterministic audit logging with rule IDs for every decision.

---

## 2. Business Rules Summary

- **BR-01 (LTV Limits)**: ≤₹30L → 90%, ₹30L–₹75L → 80%, >₹75L → 75%.
- **BR-02 (Max Tenure)**: min(30 years, 70 − age at sanction).
- **BR-03 (EMI/Income Ratio)**: 55% if combined income ≤ ₹1L; 60% if > ₹1L.
- **BR-04 (Co-applicant Weight)**: SPOUSE = 100%, OTHER = 50%.
- **BR-05 (Credit Spread)**: ≥775 → +0.50%, 725–774 → +0.90%, <725 → +1.40%.
- **BR-06 (Women Concession)**: −0.05% for primary female applicant.
- **BR-07 (Sanction Amount)**: min(LTV Limit, Eligibility Limit).
- **BR-08 (Processing Fee)**: 0.5% of sanction, min ₹10,000, max ₹25,000 + 18% GST; split 50% at sanction, 50% at first disbursal.
- **BR-09 (Sanction Validity)**: 6 calendar months validity.
- **BR-10 (PMT Eligibility)**: Loan amount derived from maximum permissible EMI.
- **BR-11 (Own Contribution)**: Fully paid upfront before any tranche release.
- **BR-12 (Stage Certification Coverage)**: Cumulative certified % ≥ cumulative demanded %.
- **BR-13 (Sanction Cap)**: Cumulative disbursements capped at sanctioned amount; excess lapses.
- **BR-14 (Delinquency Freeze)**: Release frozen upon 3 consecutive missed EMIs.
- **BR-15 (Release SLA)**: Tranche release within 7 days of demand/certification.
- **BR-16 (Certification Freshness)**: Certification validity capped at 90 days.
- **BR-17 (Pre-EMI Accrual)**: Daily accrual actual/365 per released tranche: `P × (rate/365) × days`.
- **BR-18 (Moratorium)**: Pre-EMI continues until final disbursement or month 36.
- **BR-19 (Conversion to Full EMI)**: Starts 1st of month following final tranche or month 37.
- **BR-20 (EMI on Disbursed Principal)**: Computed strictly on total disbursed principal.
- **BR-21 (Rate Reset)**: Constant EMI with adjusted tenure; capped by borrower age 70.
- **BR-22 (Part Prepayment)**: Min ₹50,000; default reduces tenure, optional reduces EMI.
- **BR-23 (Full Prepayment)**: Zero foreclosure charges; closes loan account.
- **BR-24 (Penal Interest)**: 2% per month on overdue EMI after 15-day grace period.
- **BR-25 (Month 36 Lapse)**: Undisbursed sanctioned amount lapses; loan converts on disbursed balance.
- **BR-26 (Tranche Precedence)**: Short-circuit check order: BR-09 → BR-11 → BR-12/BR-16 → BR-13 → BR-14.
