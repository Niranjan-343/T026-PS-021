# Decision Log & Documented Ambiguities — T026-PS-021
## Home Loan Servicing Engine

### 1. Month-End Date Arithmetic
- **Problem**: Adding calendar months when destination month has fewer days (e.g., Aug 31 + 6 months).
- **Decision**: Clamped to the last calendar day of the resulting month (`calendar.monthrange`), yielding Feb 28 or Feb 29 (leap year).

### 2. Pre-EMI Day Count Basis
- **Problem**: Day count convention for leap years.
- **Decision**: Actual/365 convention strictly adhered to — numerator uses actual days elapsed between dates, denominator is fixed at 365. No intermediate rounding per day; rounded to 2 decimal places at monthly bill level.

### 3. Rate Reset Beyond Age-70 Cap (BR-21)
- **Problem**: If an upward benchmark revision causes tenure extension that exceeds the borrower's age-70 cap.
- **Decision**: Tenure is clamped to `(70 - current_age) * 12`, and the monthly EMI is recomputed upward using standard PMT formula to amortise within the capped tenure.

### 4. Tranche Release Precedence Enforcement (BR-26)
- **Problem**: When multiple release criteria fail simultaneously.
- **Decision**: Evaluated using strict short-circuit precedence order:
  1. BR-09 (Sanction Validity)
  2. BR-11 (Own Contribution)
  3. BR-12 / BR-16 (Stage Certification Coverage & Freshness)
  4. BR-13 (Sanction Cap)
  5. BR-14 (Delinquency Freeze)
  The first failing check terminates evaluation and records its specific rule ID.
