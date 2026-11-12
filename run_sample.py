"""
run_sample.py  --  T026-PS-021 Mortgage Servicing Engine
=========================================================
Usage:
    py run_sample.py <data_dir> [--output <out_dir>]

Reads: borrowers.csv, properties.csv, stage_plans.csv,
       benchmark_history.csv, holidays.csv, commands.txt
Writes: output_sample/sanction_letters.csv
        output_sample/tranche_decisions.csv
        output_sample/pre_emi_bills.csv
        output_sample/schedule.csv
        output_sample/interest_certificates.csv
"""

from __future__ import annotations
import argparse, csv, math, sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import calendar as cal_mod

getcontext().prec = 28
D = Decimal
ZERO = D("0.00")

# ── date helpers ─────────────────────────────────────────────────────────────

def add_months(d: date, n: int) -> date:
    m = d.month + n
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(d.day, cal_mod.monthrange(y, m)[1])
    return date(y, m, day)

def month_start(d: date) -> date:
    return date(d.year, d.month, 1)

def month_end(d: date) -> date:
    return date(d.year, d.month, cal_mod.monthrange(d.year, d.month)[1])

def fy_range(fy_str: str) -> Tuple[date, date]:
    y = int(fy_str[:4])
    return date(y, 4, 1), date(y + 1, 3, 31)

def months_between(d1: date, d2: date) -> int:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)

def fmt(v) -> str:
    return f"{D(str(v)):.2f}"

# ── domain dataclasses ────────────────────────────────────────────────────────

@dataclass
class Borrower:
    borrower_id: str
    name: str
    gender: str
    dob: date
    income: Decimal
    credit_score: int
    co_relation: str
    co_income: Decimal

@dataclass
class Property:
    property_id: str
    borrower_id: str
    agreement_value: Decimal
    stamp_reg: Decimal

@dataclass
class StagePlan:
    property_id: str
    stage_no: int
    stage_name: str
    cumulative_pct: int

@dataclass
class Tranche:
    demand_id: str
    demand_date: date
    demand_cumulative_pct: int
    demand_amount: Decimal
    release_date: Optional[date] = None
    released_amount: Decimal = ZERO
    oc_used: Decimal = ZERO
    decision: str = "PENDING"
    rule_ids: str = ""
    sla_flag: str = "SLA_OK"

@dataclass
class PreEmiBill:
    bill_date: date
    period_from: date
    period_to: date
    interest: Decimal
    status: str = "PAID"

@dataclass
class EmiRow:
    version: int
    emi_no: int
    due_date: date
    rate_pct: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal
    status: str = "PENDING"

@dataclass
class LoanAccount:
    loan_id: str
    borrower_id: str
    property_id: str
    sanctioned_amount: Decimal
    tenure_months: int
    rate_pct: Decimal
    spread_pct: Decimal
    sanction_date: date
    valid_until: date
    processing_fee: Decimal
    fee_gst: Decimal
    ltv_pct: Decimal
    ltv_amount: Decimal
    eligibility_amount: Decimal
    oc_balance: Decimal = ZERO
    oc_paid_total: Decimal = ZERO
    total_disbursed: Decimal = ZERO       # outstanding balance (reduced by prepays)
    cumulative_released: Decimal = ZERO   # gross bank money ever released (never reduced)
    disbursements: List[Tranche] = field(default_factory=list)
    pre_emi_bills: List[PreEmiBill] = field(default_factory=list)
    pre_emi_prepays: List[Tuple[date, Decimal]] = field(default_factory=list)
    rate_history: List[Tuple[date, Decimal]] = field(default_factory=list)
    emi_schedule: List[EmiRow] = field(default_factory=list)
    paid_emis: List[EmiRow] = field(default_factory=list)
    schedule_versions: List[EmiRow] = field(default_factory=list)
    emi_version: int = 0
    certs: Dict[int, date] = field(default_factory=dict)
    converted: bool = False
    closed: bool = False
    first_release_month: Optional[date] = None
    last_monthly_job: Optional[date] = None
    current_rate: Decimal = ZERO
    spread_pct_live: Decimal = ZERO
    demands: Dict[str, Tranche] = field(default_factory=dict)
    missed_bills: Dict[date, bool] = field(default_factory=dict)
    final_stage_released: bool = False

# ── financial calculations ────────────────────────────────────────────────────

def benchmark_on(bmarks: List[Tuple[date, Decimal]], d: date) -> Decimal:
    rate = bmarks[0][1]
    for eff, r in bmarks:
        if eff <= d:
            rate = r
    return rate

def compute_ltv(av: Decimal) -> Tuple[Decimal, Decimal]:
    if av <= D("3000000"):
        pct = D("90")
    elif av <= D("7500000"):
        pct = D("80")
    else:
        pct = D("75")
    return pct, (av * pct / 100).quantize(D("0.01"), ROUND_HALF_UP)

def compute_spread(credit_score: int) -> Decimal:
    if credit_score >= 775:
        return D("0.50")
    elif credit_score >= 725:
        return D("0.90")
    else:
        return D("1.40")

def pmt(principal: Decimal, annual_rate: Decimal, n: int) -> Decimal:
    if n <= 0 or principal <= 0:
        return ZERO
    r = float(annual_rate) / 100 / 12
    p = float(principal)
    if r == 0:
        return (principal / n).quantize(D("1"), ROUND_HALF_UP)
    emi_f = p * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return D(str(emi_f)).quantize(D("1"), ROUND_HALF_UP)

def max_loan_by_income(nmi: Decimal, annual_rate: Decimal, n: int) -> Decimal:
    cap = D("0.55") if nmi <= D("100000") else D("0.60")
    max_emi = (nmi * cap).quantize(D("0.01"), ROUND_HALF_UP)
    r = float(annual_rate) / 100 / 12
    if r == 0:
        return (max_emi * n).quantize(D("0.01"), ROUND_HALF_UP)
    p = float(max_emi) * ((1 + r) ** n - 1) / (r * (1 + r) ** n)
    return D(str(p)).quantize(D("0.01"), ROUND_HALF_UP)

def seventieth_birthday(dob: date) -> date:
    day = min(dob.day, cal_mod.monthrange(dob.year + 70, dob.month)[1])
    return date(dob.year + 70, dob.month, day)

def max_tenure_months(dob: date, sanction_date: date) -> int:
    seventieth = seventieth_birthday(dob)
    if seventieth <= sanction_date:
        return 0
    return min(months_between(sanction_date, seventieth), 360)

def max_emis_before_70(first_due: date, dob: date) -> int:
    seventieth = seventieth_birthday(dob)
    if first_due >= seventieth:
        return 0
    m = months_between(first_due, seventieth) + 1
    while m > 0 and add_months(first_due, m - 1) >= seventieth:
        m -= 1
    return m

def processing_fee(sanctioned: Decimal) -> Tuple[Decimal, Decimal]:
    fee = (sanctioned * D("0.005")).quantize(D("0.01"), ROUND_HALF_UP)
    fee = max(D("10000"), min(D("25000"), fee))
    gst = (fee * D("0.18")).quantize(D("0.01"), ROUND_HALF_UP)
    return fee, gst

def generate_schedule(loan_id: str, principal: Decimal, annual_rate: Decimal,
                       n: int, first_due: date, version: int,
                       fixed_emi: Optional[Decimal] = None) -> List[EmiRow]:
    rows: List[EmiRow] = []
    if n <= 0 or principal <= ZERO:
        return rows
    emi_amt = fixed_emi if fixed_emi is not None else pmt(principal, annual_rate, n)
    balance = principal.quantize(D("0.01"), ROUND_HALF_UP)
    r = annual_rate / 100 / 12
    for i in range(1, n + 1):
        interest = (balance * r).quantize(D("0.01"), ROUND_HALF_UP)
        principal_part = emi_amt - interest
        if i == n or principal_part > balance:
            principal_part = balance
            emi_actual = principal_part + interest
        else:
            emi_actual = emi_amt
        balance = max(ZERO, (balance - principal_part).quantize(D("0.01"), ROUND_HALF_UP))
        due = add_months(first_due, i - 1)
        rows.append(EmiRow(
            version=version, emi_no=i, due_date=due,
            rate_pct=annual_rate, emi=emi_actual,
            interest=interest, principal=principal_part, balance=balance
        ))
        if balance <= ZERO:
            break
    return rows

def new_tenure_for_reset(remaining: Decimal, new_rate: Decimal, curr_emi: Decimal) -> int:
    r = float(new_rate) / 100 / 12
    P = float(remaining)
    E = float(curr_emi)
    if r == 0 or E <= 0:
        return 360
    val = 1 - r * P / E
    if val <= 0:
        return 999
    n = -math.log(val) / math.log(1 + r)
    return math.ceil(n)

# ── engine ────────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self):
        self.borrowers: Dict[str, Borrower] = {}
        self.properties: Dict[str, Property] = {}
        self.stage_plans: Dict[str, List[StagePlan]] = defaultdict(list)
        self.benchmarks: List[Tuple[date, Decimal]] = []
        self.holidays: set = set()
        self.loans: Dict[str, LoanAccount] = {}
        self.data_dir: Path = Path(".")

        self.out_sanctions: List[dict] = []
        self.out_tranches: List[dict] = []
        self.out_pre_emi: List[dict] = []
        self.out_schedule: List[dict] = []
        self.out_certs: List[dict] = []

    # ── loaders ──────────────────────────────────────────────────────────────

    def load_borrowers(self, fname):
        with open(self.data_dir / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.borrowers[r["borrower_id"]] = Borrower(
                    borrower_id=r["borrower_id"], name=r["borrower_name"],
                    gender=r["gender"].upper(), dob=date.fromisoformat(r["date_of_birth"]),
                    income=D(r["monthly_net_income"]), credit_score=int(r["credit_score"]),
                    co_relation=r["co_applicant_relation"].upper(),
                    co_income=D(r["co_applicant_monthly_income"]),
                )

    def load_properties(self, fname):
        with open(self.data_dir / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.properties[r["property_id"]] = Property(
                    property_id=r["property_id"], borrower_id=r["borrower_id"],
                    agreement_value=D(r["agreement_value"]),
                    stamp_reg=D(r["stamp_and_registration"]),
                )

    def load_stage_plans(self, fname):
        with open(self.data_dir / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.stage_plans[r["property_id"]].append(StagePlan(
                    property_id=r["property_id"], stage_no=int(r["stage_no"]),
                    stage_name=r["stage_name"], cumulative_pct=int(r["cumulative_pct"]),
                ))

    def load_benchmarks(self, fname):
        with open(self.data_dir / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.benchmarks.append((date.fromisoformat(r["effective_date"]), D(r["benchmark_rate_pct"])))
        self.benchmarks.sort(key=lambda x: x[0])

    def load_holidays(self, fname):
        with open(self.data_dir / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.holidays.add(date.fromisoformat(r["holiday_date"]))

    # ── SANCTION ─────────────────────────────────────────────────────────────

    def handle_sanction(self, loan_id, borrower_id, property_id, requested, tenure_req, s_date):
        b = self.borrowers.get(borrower_id)
        prop = self.properties.get(property_id)
        reject_row = {
            "loan_id": loan_id, "borrower_id": borrower_id, "ltv_pct": "0.00",
            "ltv_amount": "0.00", "eligibility_amount": "0.00", "sanctioned_amount": "0.00",
            "tenure_months": 0, "spread_pct": "0.00", "rate_pct": "0.00",
            "processing_fee": "0.00", "fee_gst": "0.00", "valid_until": "", "decision": "REJECTED"
        }
        if not b or not prop:
            self.out_sanctions.append(reject_row)
            return

        ltv_pct, ltv_amount = compute_ltv(prop.agreement_value)
        bmark = benchmark_on(self.benchmarks, s_date)
        spread = compute_spread(b.credit_score)
        women_disc = D("0.05") if b.gender == "F" else ZERO
        rate = bmark + spread - women_disc

        tenure = min(tenure_req, max_tenure_months(b.dob, s_date))
        if tenure <= 0:
            self.out_sanctions.append({**reject_row, "ltv_pct": fmt(ltv_pct),
                "ltv_amount": fmt(ltv_amount), "spread_pct": fmt(spread), "rate_pct": fmt(rate)})
            return

        # NMI
        if b.co_relation == "SPOUSE":
            nmi = b.income + b.co_income
        elif b.co_relation in ("PARENT", "SIBLING"):
            nmi = b.income + (b.co_income * D("0.5")).quantize(D("0.01"), ROUND_HALF_UP)
        else:
            nmi = b.income

        elig = max_loan_by_income(nmi, rate, tenure)
        sanctioned = min(requested, ltv_amount, elig).quantize(D("0.01"), ROUND_HALF_UP)
        fee, gst = processing_fee(sanctioned)
        valid_until = add_months(s_date, 6)

        loan = LoanAccount(
            loan_id=loan_id, borrower_id=borrower_id, property_id=property_id,
            sanctioned_amount=sanctioned, tenure_months=tenure,
            rate_pct=rate, spread_pct=spread, spread_pct_live=spread,
            sanction_date=s_date, valid_until=valid_until,
            processing_fee=fee, fee_gst=gst,
            ltv_pct=ltv_pct, ltv_amount=ltv_amount,
            eligibility_amount=elig.quantize(D("0.01"), ROUND_HALF_UP),
            current_rate=rate,
            rate_history=[(s_date, rate)],
            oc_balance=(prop.agreement_value - sanctioned).quantize(D("0.01"), ROUND_HALF_UP),
        )
        self.loans[loan_id] = loan
        self.out_sanctions.append({
            "loan_id": loan_id, "borrower_id": borrower_id,
            "ltv_pct": fmt(ltv_pct), "ltv_amount": fmt(ltv_amount),
            "eligibility_amount": fmt(elig), "sanctioned_amount": fmt(sanctioned),
            "tenure_months": tenure, "spread_pct": fmt(spread), "rate_pct": fmt(rate),
            "processing_fee": fmt(fee), "fee_gst": fmt(gst),
            "valid_until": valid_until.isoformat(), "decision": "APPROVED"
        })

    # ── OWN_CONTRIBUTION ─────────────────────────────────────────────────────

    def handle_own_contribution(self, loan_id, ev_date, amount):
        if loan_id in self.loans:
            self.loans[loan_id].oc_paid_total += amount

    # ── STAGE_CERTIFIED ───────────────────────────────────────────────────────

    def handle_stage_certified(self, loan_id, cert_id, cum_pct, cert_date):
        loan = self.loans.get(loan_id)
        if not loan:
            return
        # Keep best (most recent) cert for each pct level, also propagate lower levels
        for pct in range(1, cum_pct + 1):
            existing = loan.certs.get(pct)
            if existing is None or cert_date > existing:
                loan.certs[pct] = cert_date
        # Also store exact pct
        loan.certs[cum_pct] = cert_date

    # ── DEMAND ────────────────────────────────────────────────────────────────

    def handle_demand(self, loan_id, demand_id, cum_pct, amount, demand_date):
        loan = self.loans.get(loan_id)
        if not loan:
            return
        t = Tranche(demand_id=demand_id, demand_date=demand_date,
                    demand_cumulative_pct=cum_pct, demand_amount=amount)
        loan.demands[demand_id] = t
        loan.disbursements.append(t)

    # ── RELEASE ───────────────────────────────────────────────────────────────

    def handle_release(self, loan_id, demand_id, release_date):
        if loan_id not in self.loans:
            self.out_tranches.append({
                "loan_id": loan_id, "demand_id": demand_id,
                "date": release_date.isoformat(), "decision": "REJECTED",
                "demand_amount": "0.00", "own_contribution_used": "0.00",
                "released_amount": "0.00", "cumulative_disbursed": "0.00",
                "rule_ids": "UNKNOWN_LOAN", "sla_flag": "SLA_OK"
            })
            return

        loan = self.loans[loan_id]
        self._run_monthly_jobs_upto(loan, release_date)

        t = loan.demands.get(demand_id)
        if not t:
            self._append_tranche(loan, demand_id, release_date, "REJECTED",
                                  ZERO, ZERO, ZERO, "UNKNOWN_DEMAND", "SLA_OK")
            return

        def reject(rule):
            self._append_tranche(loan, demand_id, release_date, "REJECTED",
                                  t.demand_amount, ZERO, ZERO, rule, "SLA_OK")
            t.decision = "REJECTED"; t.rule_ids = rule

        # BR-09: sanction validity — only blocks if NO bank money released yet
        # Once first bank tranche is out the sanction is "availed"; validity no longer blocks.
        if release_date > loan.valid_until and loan.cumulative_released == ZERO:
            reject("BR-09"); return

        # BR-25: moratorium ended / converted (month-36 lapse or final stage already released)
        if loan.converted:
            reject("BR-25"); return

        # BR-10: OC paid?
        if loan.oc_balance > ZERO and loan.oc_paid_total <= ZERO and loan.cumulative_released == ZERO:
            reject("BR-10"); return

        # BR-12 + BR-13: cert check
        # Find latest cert with pct >= demand pct
        cert_date = None
        for pct, cd in loan.certs.items():
            if pct >= t.demand_cumulative_pct:
                if cert_date is None or cd > cert_date:
                    cert_date = cd

        if cert_date is None:
            reject("BR-12"); return

        cert_age = (release_date - cert_date).days
        if cert_age > 90:
            reject("BR-13"); return

        # BR-24: 3 consecutive missed?
        if self._consecutive_missed(loan, release_date) >= 3:
            reject("BR-24"); return

        # BR-16: sanction cap — use cumulative_released (gross), not outstanding balance
        remaining = (loan.sanctioned_amount - loan.cumulative_released).quantize(D("0.01"), ROUND_HALF_UP)
        if remaining <= ZERO:
            reject("BR-16"); return

        # OC absorption
        oc_used = min(loan.oc_balance, t.demand_amount).quantize(D("0.01"), ROUND_HALF_UP)
        bank_part = (t.demand_amount - oc_used).quantize(D("0.01"), ROUND_HALF_UP)
        bank_part = min(bank_part, remaining).quantize(D("0.01"), ROUND_HALF_UP)

        # Recompute actual OC used given capped bank release
        oc_used = (t.demand_amount - bank_part).quantize(D("0.01"), ROUND_HALF_UP)
        oc_used = min(oc_used, loan.oc_balance).quantize(D("0.01"), ROUND_HALF_UP)

        loan.oc_balance = max(ZERO, (loan.oc_balance - oc_used)).quantize(D("0.01"), ROUND_HALF_UP)
        loan.total_disbursed = (loan.total_disbursed + bank_part).quantize(D("0.01"), ROUND_HALF_UP)
        # Track gross cumulative bank money (never reduced by prepayments — used for BR-16 cap)
        loan.cumulative_released = (loan.cumulative_released + bank_part).quantize(D("0.01"), ROUND_HALF_UP)

        # SLA flag
        sla_ref = max(t.demand_date, cert_date)
        sla_flag = "SLA_OK" if (release_date - sla_ref).days <= 7 else "SLA_BREACH"

        rule_ids = "BR-12,BR-13,BR-26"
        if bank_part < (t.demand_amount - oc_used):
            rule_ids += ",BR-16"

        decision = "RELEASED" if bank_part >= ZERO else "REJECTED"
        # If demand was fully covered by OC, it's still RELEASED with bank=0
        if bank_part == ZERO and oc_used == t.demand_amount:
            decision = "RELEASED"
        elif bank_part < (t.demand_amount - oc_used):
            decision = "PARTIAL"

        t.release_date = release_date
        t.released_amount = bank_part
        t.oc_used = oc_used
        t.decision = decision
        t.rule_ids = rule_ids
        t.sla_flag = sla_flag

        # Bug 3 fix: only set first_release_month when actual bank money goes out
        if loan.first_release_month is None and bank_part > ZERO:
            loan.first_release_month = month_start(release_date)

        # Check final stage
        prop = self.properties.get(loan.property_id)
        if prop:
            stages = self.stage_plans.get(prop.property_id, [])
            max_pct = max((s.cumulative_pct for s in stages), default=100)
            if t.demand_cumulative_pct >= max_pct:
                loan.final_stage_released = True

        self._append_tranche(loan, demand_id, release_date, decision,
                              t.demand_amount, oc_used, bank_part, rule_ids, sla_flag)

    def _append_tranche(self, loan, demand_id, rel_date, decision,
                         demand_amt, oc_used, released, rule_ids, sla_flag):
        self.out_tranches.append({
            "loan_id": loan.loan_id, "demand_id": demand_id,
            "date": rel_date.isoformat(), "decision": decision,
            "demand_amount": fmt(demand_amt), "own_contribution_used": fmt(oc_used),
            "released_amount": fmt(released),
            "cumulative_disbursed": fmt(loan.cumulative_released),
            "rule_ids": rule_ids, "sla_flag": sla_flag
        })

    # ── PREPAY ────────────────────────────────────────────────────────────────

    def handle_prepay(self, loan_id, prepay_date, amount, mode):
        loan = self.loans.get(loan_id)
        if not loan or loan.closed:
            return
        self._run_monthly_jobs_upto(loan, prepay_date)

        if amount < D("50000"):
            return  # BR-19: minimum 50000

        if not loan.converted:
            # Pre-EMI: reduce disbursed balance and log for daily interest calc
            effective = min(amount, loan.total_disbursed).quantize(D("0.01"), ROUND_HALF_UP)
            loan.total_disbursed = max(ZERO, (loan.total_disbursed - amount)).quantize(D("0.01"), ROUND_HALF_UP)
            if effective > ZERO:
                loan.pre_emi_prepays.append((prepay_date, effective))
            return

        # Post-conversion
        outstanding = self._outstanding_balance(loan, prepay_date)
        new_principal = max(ZERO, outstanding - amount).quantize(D("0.01"), ROUND_HALF_UP)
        if new_principal <= ZERO:
            for e in loan.emi_schedule:
                if e.status == "PENDING":
                    e.status = "PAID"
            loan.closed = True
            return

        next_dues = [e.due_date for e in loan.emi_schedule if e.due_date > prepay_date]
        if not next_dues:
            loan.closed = True
            return
        first_due = min(next_dues)
        curr_emi = next((e.emi for e in loan.emi_schedule if e.due_date == first_due),
                        loan.emi_schedule[0].emi if loan.emi_schedule else ZERO)

        loan.emi_version += 1
        if mode == "REDUCE_EMI":
            rem = self._remaining_months(loan, prepay_date)
            if rem <= 0:
                loan.closed = True
                return
            new_rows = generate_schedule(
                loan.loan_id, new_principal, loan.current_rate, rem, first_due, loan.emi_version)
        else:  # REDUCE_TENURE
            n = new_tenure_for_reset(new_principal, loan.current_rate, curr_emi)
            if n <= 0:
                loan.closed = True
                return
            b = self.borrowers.get(loan.borrower_id)
            fixed = curr_emi
            if b:
                max_m = max_emis_before_70(first_due, b.dob)
                if max_m > 0 and n > max_m:
                    n = max_m
                    fixed = None
            new_rows = generate_schedule(
                loan.loan_id, new_principal, loan.current_rate, n, first_due, loan.emi_version,
                fixed_emi=fixed)
        self._replace_schedule(loan, new_rows)

    # ── MISSED ────────────────────────────────────────────────────────────────

    def handle_missed(self, loan_id, bill_date):
        loan = self.loans.get(loan_id)
        if not loan:
            return
        loan.missed_bills[bill_date] = True
        for b in loan.pre_emi_bills:
            if b.bill_date == bill_date:
                b.status = "OVERDUE"
                for row in self.out_pre_emi:
                    if row["loan_id"] == loan_id and row["bill_date"] == bill_date.isoformat():
                        row["status"] = "OVERDUE"
        for e in loan.emi_schedule:
            if e.due_date == bill_date:
                e.status = "OVERDUE"

    # ── PAYMENT ───────────────────────────────────────────────────────────────

    def handle_payment(self, loan_id, pay_date, amount_or_all):
        loan = self.loans.get(loan_id)
        if not loan:
            return
        for b in loan.pre_emi_bills:
            if b.status in ("OVERDUE", "MISSED"):
                b.status = "PAID"
                # update out_pre_emi
                for row in self.out_pre_emi:
                    if row["loan_id"] == loan_id and row["bill_date"] == b.bill_date.isoformat():
                        row["status"] = "PAID"
        for e in loan.emi_schedule:
            if e.status in ("OVERDUE", "MISSED"):
                e.status = "PAID"
        loan.missed_bills.clear()

    # ── RUN_TO ────────────────────────────────────────────────────────────────

    def handle_run_to(self, run_date):
        for loan in self.loans.values():
            self._run_monthly_jobs_upto(loan, run_date)

    # ── monthly jobs ──────────────────────────────────────────────────────────

    def _run_monthly_jobs_upto(self, loan: LoanAccount, up_to: date):
        if loan.last_monthly_job is None:
            start = month_start(loan.sanction_date)
        else:
            start = add_months(loan.last_monthly_job, 1)

        cur = start
        while month_start(cur) <= month_start(up_to):
            self._run_month(loan, cur)
            cur = add_months(month_start(cur), 1)
        loan.last_monthly_job = month_start(up_to)

    def _run_month(self, loan: LoanAccount, m_ref: date):
        m_start = month_start(m_ref)

        # Rate reset on 1st
        self._check_rate_reset(loan, m_start)

        # Month-36 conversion check
        if loan.first_release_month and not loan.converted:
            elapsed = months_between(loan.first_release_month, m_start)
            if elapsed >= 36 or loan.final_stage_released:
                if loan.total_disbursed > ZERO:
                    self._convert(loan, m_start)

        if loan.closed:
            return

        bill_date = date(m_start.year, m_start.month, 5)

        if not loan.converted and loan.total_disbursed > ZERO:
            # Pre-EMI bill for previous month
            prev = add_months(m_start, -1)
            prev_start = month_start(prev)
            prev_end   = month_end(prev)
            interest = self._compute_pre_emi(loan, prev_start, prev_end)
            if interest > ZERO:
                is_missed = bill_date in loan.missed_bills
                status = "OVERDUE" if is_missed else "PAID"
                bill = PreEmiBill(bill_date, prev_start, prev_end, interest, status)
                loan.pre_emi_bills.append(bill)
                self.out_pre_emi.append({
                    "loan_id": loan.loan_id,
                    "bill_date": bill_date.isoformat(),
                    "period_from": prev_start.isoformat(),
                    "period_to": prev_end.isoformat(),
                    "interest": fmt(interest),
                    "status": status,
                })

        elif loan.converted and not loan.closed:
            # Mark EMI due this month
            for emi in loan.emi_schedule:
                if emi.due_date.year == m_start.year and emi.due_date.month == m_start.month:
                    if emi.due_date in loan.missed_bills or bill_date in loan.missed_bills:
                        emi.status = "OVERDUE"
                    else:
                        if emi.status == "PENDING":
                            emi.status = "PAID"

    def _replace_schedule(self, loan: LoanAccount, new_rows: List[EmiRow]):
        for e in loan.emi_schedule:
            if e.status == "PAID":
                loan.paid_emis.append(e)
        loan.emi_schedule = new_rows
        loan.schedule_versions.extend(new_rows)

    def _rate_on(self, loan: LoanAccount, d: date) -> Decimal:
        rate = loan.rate_pct
        for eff, r in loan.rate_history:
            if eff <= d:
                rate = r
        return rate

    def _check_rate_reset(self, loan: LoanAccount, m_start: date):
        for eff_date, brate in self.benchmarks:
            reset_month = month_start(add_months(eff_date, 1))
            if reset_month == m_start:
                b = self.borrowers.get(loan.borrower_id)
                women_disc = D("0.05") if b and b.gender == "F" else ZERO
                new_rate = brate + loan.spread_pct_live - women_disc
                if new_rate == loan.current_rate:
                    continue
                loan.current_rate = new_rate
                loan.rate_history.append((m_start, new_rate))
                if loan.converted and not loan.closed and loan.emi_schedule:
                    self._do_rate_reset(loan, m_start, new_rate)

    def _do_rate_reset(self, loan: LoanAccount, reset_date: date, new_rate: Decimal):
        outstanding = self._outstanding_balance(loan, reset_date)
        if outstanding <= ZERO:
            return
        first_due = date(reset_date.year, reset_date.month, 5)
        curr_emi = None
        for e in loan.emi_schedule:
            if e.due_date >= reset_date:
                curr_emi = e.emi
                first_due = e.due_date
                break
        if curr_emi is None:
            return

        n = new_tenure_for_reset(outstanding, new_rate, curr_emi)
        fixed = curr_emi
        b = self.borrowers.get(loan.borrower_id)
        if b:
            max_m = max_emis_before_70(first_due, b.dob)
            if max_m > 0 and n > max_m:
                n = max_m
                fixed = pmt(outstanding, new_rate, n)

        if n <= 0:
            return
        loan.emi_version += 1
        new_rows = generate_schedule(
            loan.loan_id, outstanding, new_rate, n, first_due, loan.emi_version,
            fixed_emi=fixed)
        self._replace_schedule(loan, new_rows)

    def _convert(self, loan: LoanAccount, conv_date: date):
        if loan.converted:
            return
        loan.converted = True
        first_due = date(conv_date.year, conv_date.month, 5)
        months_used = months_between(loan.sanction_date, conv_date)
        remaining = max(1, loan.tenure_months - months_used)
        b = self.borrowers.get(loan.borrower_id)
        if b:
            max_m = max_emis_before_70(first_due, b.dob)
            if max_m > 0:
                remaining = min(remaining, max_m)
        loan.emi_version = 1
        new_rows = generate_schedule(
            loan.loan_id, loan.total_disbursed, loan.current_rate,
            remaining, first_due, loan.emi_version)
        self._replace_schedule(loan, new_rows)

    def _compute_pre_emi(self, loan: LoanAccount, pf: date, pt: date) -> Decimal:
        total = ZERO
        cur = pf
        while cur <= pt:
            disb = sum(
                t.released_amount for t in loan.disbursements
                if t.release_date and t.release_date <= cur and t.released_amount > ZERO
            )
            prep = sum(amt for pd, amt in loan.pre_emi_prepays if pd <= cur)
            bal = max(ZERO, disb - prep)
            if bal > ZERO:
                rate = self._rate_on(loan, cur)
                daily = (D(str(bal)) * rate / 100 / 365).quantize(D("0.000001"), ROUND_HALF_UP)
                total += daily
            cur += timedelta(days=1)
        return total.quantize(D("0.01"), ROUND_HALF_UP)

    def _consecutive_missed(self, loan: LoanAccount, as_of: date) -> int:
        bills = sorted(
            [(b.bill_date, b.status) for b in loan.pre_emi_bills if b.bill_date <= as_of] +
            [(e.due_date, e.status) for e in loan.paid_emis + loan.emi_schedule if e.due_date <= as_of],
            key=lambda x: x[0]
        )
        count = 0
        for _, status in reversed(bills):
            if status in ("OVERDUE", "MISSED"):
                count += 1
            elif status == "PAID":
                break
        return count

    def _outstanding_balance(self, loan: LoanAccount, as_of: date) -> Decimal:
        last_paid_bal = None
        for e in loan.emi_schedule:
            if e.due_date <= as_of and e.status == "PAID":
                last_paid_bal = e.balance
        if last_paid_bal is not None:
            return last_paid_bal
        # No EMIs paid yet in current version — return principal before next EMI
        for e in loan.emi_schedule:
            if e.due_date > as_of:
                return (e.balance + e.principal).quantize(D("0.01"), ROUND_HALF_UP)
        return loan.total_disbursed

    def _remaining_months(self, loan: LoanAccount, as_of: date) -> int:
        return sum(1 for e in loan.emi_schedule if e.due_date > as_of)

    # ── PRINT handlers ────────────────────────────────────────────────────────

    def print_schedule(self, loan_id, as_of):
        loan = self.loans.get(loan_id)
        if not loan:
            return
        self._run_monthly_jobs_upto(loan, as_of)
        for r in loan.schedule_versions:
            self.out_schedule.append({
                "loan_id": loan_id, "version": r.version, "emi_no": r.emi_no,
                "due_date": r.due_date.isoformat(), "rate_pct": fmt(r.rate_pct),
                "emi": fmt(r.emi), "interest": fmt(r.interest),
                "principal": fmt(r.principal), "balance": fmt(r.balance),
            })

    def print_interest_cert(self, loan_id, fy_str):
        loan = self.loans.get(loan_id)
        if not loan:
            self.out_certs.append({"loan_id": loan_id, "fy": fy_str,
                "pre_emi_interest": "0.00", "emi_interest": "0.00", "principal_repaid": "0.00"})
            return
        fs, fe = fy_range(fy_str)
        all_paid_emis = loan.paid_emis + [e for e in loan.emi_schedule if e.status == "PAID"]
        pre = sum(b.interest for b in loan.pre_emi_bills if fs <= b.bill_date <= fe and b.status == "PAID")
        ei  = sum(e.interest for e in all_paid_emis if fs <= e.due_date <= fe)
        pr  = sum(e.principal for e in all_paid_emis if fs <= e.due_date <= fe)
        self.out_certs.append({
            "loan_id": loan_id, "fy": fy_str,
            "pre_emi_interest": fmt(pre), "emi_interest": fmt(ei), "principal_repaid": fmt(pr),
        })

    # ── command dispatcher ────────────────────────────────────────────────────

    def process(self, commands_file: Path):
        with open(commands_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = line.split("|")
                cmd = p[0].upper()
                try:
                    if cmd == "LOAD_BORROWERS":        self.load_borrowers(p[1])
                    elif cmd == "LOAD_PROPERTIES":     self.load_properties(p[1])
                    elif cmd == "LOAD_STAGE_PLANS":    self.load_stage_plans(p[1])
                    elif cmd == "LOAD_BENCHMARKS":     self.load_benchmarks(p[1])
                    elif cmd == "LOAD_HOLIDAYS":       self.load_holidays(p[1])
                    elif cmd == "SANCTION_REQUEST":
                        self.handle_sanction(p[1], p[2], p[3], D(p[4]), int(p[5]), date.fromisoformat(p[6]))
                    elif cmd == "OWN_CONTRIBUTION":
                        self.handle_own_contribution(p[1], date.fromisoformat(p[2]), D(p[3]))
                    elif cmd == "STAGE_CERTIFIED":
                        self.handle_stage_certified(p[1], p[2], int(p[3]), date.fromisoformat(p[4]))
                    elif cmd == "DEMAND":
                        self.handle_demand(p[1], p[2], int(p[3]), D(p[4]), date.fromisoformat(p[5]))
                    elif cmd == "RELEASE":
                        self.handle_release(p[1], p[2], date.fromisoformat(p[3]))
                    elif cmd == "PREPAY":
                        self.handle_prepay(p[1], date.fromisoformat(p[2]), D(p[3]), p[4].upper())
                    elif cmd == "MISSED":
                        self.handle_missed(p[1], date.fromisoformat(p[2]))
                    elif cmd == "PAYMENT":
                        self.handle_payment(p[1], date.fromisoformat(p[2]), p[3])
                    elif cmd == "RUN_TO":
                        self.handle_run_to(date.fromisoformat(p[1]))
                    elif cmd == "PRINT_DISBURSEMENT_LEDGER":
                        pass  # captured in tranche_decisions
                    elif cmd == "PRINT_SCHEDULE":
                        self.print_schedule(p[1], date.fromisoformat(p[2]))
                    elif cmd == "PRINT_INTEREST_CERT":
                        self.print_interest_cert(p[1], p[2])
                except Exception as e:
                    print(f"  [WARN] Line '{line}': {e}", file=sys.stderr)

    # ── write outputs ─────────────────────────────────────────────────────────

    def write_outputs(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)

        def w(name, cols, rows):
            with open(out_dir / name, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
                csv.DictWriter(f, fieldnames=cols).writerows(rows)
            print(f"  {len(rows):>5} rows  ->  {name}")

        w("sanction_letters.csv",
          ["loan_id","borrower_id","ltv_pct","ltv_amount","eligibility_amount",
           "sanctioned_amount","tenure_months","spread_pct","rate_pct",
           "processing_fee","fee_gst","valid_until","decision"],
          self.out_sanctions)

        w("tranche_decisions.csv",
          ["loan_id","demand_id","date","decision","demand_amount",
           "own_contribution_used","released_amount","cumulative_disbursed",
           "rule_ids","sla_flag"],
          self.out_tranches)

        w("pre_emi_bills.csv",
          ["loan_id","bill_date","period_from","period_to","interest","status"],
          self.out_pre_emi)

        w("schedule.csv",
          ["loan_id","version","emi_no","due_date","rate_pct",
           "emi","interest","principal","balance"],
          self.out_schedule)

        w("interest_certificates.csv",
          ["loan_id","fy","pre_emi_interest","emi_interest","principal_repaid"],
          self.out_certs)


def main():
    ap = argparse.ArgumentParser(description="T026-PS-021 Mortgage Engine")
    ap.add_argument("data_dir", help="Directory with CSV files + commands.txt")
    ap.add_argument("--output", default="output_sample")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    out_dir  = Path(args.output).resolve()
    print(f"Input : {data_dir}")
    print(f"Output: {out_dir}\n")

    eng = Engine()
    eng.data_dir = data_dir
    eng.process(data_dir / "commands.txt")
    eng.write_outputs(out_dir)
    print("\nComplete.")

if __name__ == "__main__":
    main()
