"""
tests/test_phase2.py — Phase 2 Unit and Integration Tests (BR-11 to BR-26)
Team T026 / PS-021
Total: 58 tests.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from src.models.borrower import Borrower
from src.models.property import Property, StageItem
from src.models.loan import LoanAccount, SanctionResult, DisburseRecord
from src.rules.business_rules import BusinessRules
from src.calculations.disbursement import (
    check_tranche_release,
    calculate_release_amount,
    consecutive_missed_emis,
)
from src.calculations.pre_emi import (
    daily_rate,
    accrual_for_tranche,
    generate_pre_emi_bill,
    bill_date_for_month,
    period_for_billing_month,
)
from src.calculations.emi_schedule import (
    compute_emi,
    generate_schedule,
    apply_rate_reset,
    recalculate_schedule_after_prepayment,
    next_emi_due_date_after,
)
from src.engine.loan_engine import LoanEngine


def _make_dummy_loan(sanctioned_amount=Decimal('3000000'), tenure_months=240, rate=Decimal('9.00')):
    return LoanAccount(
        loan_id="LN2024001",
        borrower_id="B001",
        property_id="P001",
        sanctioned_amount=sanctioned_amount,
        tenure_months=tenure_months,
        interest_rate=rate,
        ltv_limit=Decimal('2700000'),
        eligibility_limit=Decimal('5000000'),
        processing_fee={"total_fee": Decimal('17700')},
        gst_on_fee=Decimal('2700'),
        sanction_date=date(2024, 1, 15),
        expiry_date=date(2024, 7, 15),
    )


def _make_dummy_borrower(dob=date(1990, 6, 20), gender="F"):
    return Borrower("B001", "Meera", dob, gender, Decimal('150000'), 780)


def _make_dummy_property(val=Decimal('3000000')):
    return Property("P001", val, "Prestige", [
        StageItem("Foundation", Decimal('20')),
        StageItem("Superstructure", Decimal('30')),
        StageItem("Finishing", Decimal('50')),
    ])


class TestDisburseChecks(unittest.TestCase):
    """Tranche release checks: BR-09, BR-11, BR-12, BR-13, BR-14, BR-16, BR-26."""

    def setUp(self):
        self.loan = _make_dummy_loan()
        self.borrower = _make_dummy_borrower()

    def test_accept_cert_exactly_90_days(self):
        cert_dt = date(2024, 2, 1)
        event_dt = cert_dt + timedelta(days=90)
        decision, _, _, amt = check_tranche_release(
            loan=self.loan,
            demand_date=event_dt,
            demand_amount=Decimal('600000'),
            certification_date=cert_dt,
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=event_dt,
            cumulative_demanded_pct=Decimal('0'),
        )
        self.assertEqual(decision, "RELEASED")
        self.assertEqual(amt, Decimal('600000'))

    def test_reject_cert_over_90_days(self):
        cert_dt = date(2024, 2, 1)
        event_dt = cert_dt + timedelta(days=91)
        decision, reason, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=event_dt,
            demand_amount=Decimal('600000'),
            certification_date=cert_dt,
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=event_dt,
            cumulative_demanded_pct=Decimal('0'),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-16", rules)

    def test_reject_second_demand_without_new_cert(self):
        # 1st demand used 20%. 2nd demand brings demand to 40% but cert is still 20%
        decision, reason, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 3, 1),
            demand_amount=Decimal('600000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 3, 1),
            cumulative_demanded_pct=Decimal('20'),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-12", rules)

    def test_br09_reject_expired_sanction(self):
        event_dt = date(2024, 8, 1)  # past 2024-07-15
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=event_dt,
            demand_amount=Decimal('500000'),
            certification_date=event_dt,
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=event_dt,
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-09", rules)

    def test_br11_reject_own_contribution_not_paid(self):
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('20'),
            own_contribution_paid=False,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-11", rules)

    def test_br11_accept_own_contribution_paid(self):
        decision, _, _, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "RELEASED")

    def test_br12_reject_insufficient_certified_pct(self):
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('1000000'),  # 33.33%
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-12", rules)

    def test_br12_accept_equal_certified_and_demanded(self):
        decision, _, _, amt = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('600000'),  # 20%
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('20'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "RELEASED")
        self.assertEqual(amt, Decimal('600000'))

    def test_br13_reject_when_sanction_fully_disbursed(self):
        self.loan.disbursement_records = [
            {"amount": Decimal('3000000'), "decision": "RELEASED"}
        ]
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('100'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-13", rules)

    def test_br13_partial_release_when_demand_exceeds_remaining(self):
        self.loan.disbursement_records = [
            {"amount": Decimal('2600000'), "decision": "RELEASED"}
        ]
        decision, reason, rules, amt = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('600000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('100'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "PARTIAL")
        self.assertEqual(amt, Decimal('400000'))

    def test_br14_reject_when_three_missed_emis(self):
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('50'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
            consecutive_missed=3,
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-14", rules)

    def test_br14_allow_when_two_missed_emis(self):
        decision, _, _, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('50'),
            own_contribution_paid=True,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
            consecutive_missed=2,
        )
        self.assertEqual(decision, "RELEASED")

    def test_br26_precedence_br09_over_br11(self):
        # When both expired AND own contribution unpaid, BR-09 must be reported first
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 9, 1),
            demand_amount=Decimal('500000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('50'),
            own_contribution_paid=False,
            borrower=self.borrower,
            event_date=date(2024, 9, 1),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-09", rules)
        self.assertNotIn("BR-11", rules)

    def test_br26_precedence_br11_over_br12(self):
        # When own contribution unpaid AND cert insufficient, BR-11 must be reported first
        decision, _, rules, _ = check_tranche_release(
            loan=self.loan,
            demand_date=date(2024, 2, 1),
            demand_amount=Decimal('1000000'),
            certification_date=date(2024, 2, 1),
            certified_pct=Decimal('10'),
            own_contribution_paid=False,
            borrower=self.borrower,
            event_date=date(2024, 2, 1),
        )
        self.assertEqual(decision, "REJECTED")
        self.assertIn("BR-11", rules)
        self.assertNotIn("BR-12", rules)


class TestPreEmi(unittest.TestCase):
    """Pre-EMI billing logic: BR-17, BR-18."""

    def test_daily_rate_calculation(self):
        r = daily_rate(Decimal('9.00'))
        expected = Decimal('9.00') / Decimal('36500')
        self.assertEqual(r, expected)

    def test_accrual_for_tranche_zero_days(self):
        amt = accrual_for_tranche(Decimal('500000'), Decimal('9.00'), date(2024, 2, 1), date(2024, 2, 1))
        self.assertEqual(amt, Decimal('0'))

    def test_accrual_for_tranche_normal(self):
        # 500,000 * (9 / 100 / 365) * 10 days = 1232.88
        amt = accrual_for_tranche(Decimal('500000'), Decimal('9.00'), date(2024, 2, 1), date(2024, 2, 11))
        self.assertEqual(amt, Decimal('1232.88'))

    def test_accrual_actual_365_basis(self):
        # 365 days accrual equals annual interest
        amt = accrual_for_tranche(Decimal('1000000'), Decimal('10.00'), date(2023, 1, 1), date(2024, 1, 1))
        self.assertEqual(amt, Decimal('100000.00'))

    def test_generate_pre_emi_bill_structure(self):
        tranches = [{"amount": Decimal('500000'), "release_date": date(2024, 1, 10)}]
        bill = generate_pre_emi_bill(
            tranches, Decimal('9.00'), date(2024, 1, 1), date(2024, 1, 31),
            bill_no=1, bill_date=date(2024, 2, 5),
        )
        self.assertEqual(bill["bill_no"], 1)
        self.assertEqual(bill["bill_date"], "2024-02-05")
        self.assertIn("BR-17", bill["rules_applied"])

    def test_bill_date_always_5th(self):
        self.assertEqual(bill_date_for_month(2024, 3), date(2024, 3, 5))

    def test_period_for_billing_month_january(self):
        # January bill covers previous year's December
        p_from, p_to = period_for_billing_month(2024, 1)
        self.assertEqual(p_from, date(2023, 12, 1))
        self.assertEqual(p_to, date(2023, 12, 31))

    def test_period_for_billing_month_regular(self):
        p_from, p_to = period_for_billing_month(2024, 4)
        self.assertEqual(p_from, date(2024, 3, 1))
        self.assertEqual(p_to, date(2024, 3, 31))

    def test_multiple_tranches_in_single_bill(self):
        tranches = [
            {"amount": Decimal('300000'), "release_date": date(2024, 1, 5)},
            {"amount": Decimal('200000'), "release_date": date(2024, 1, 20)},
        ]
        bill = generate_pre_emi_bill(
            tranches, Decimal('9.00'), date(2024, 1, 1), date(2024, 1, 31),
            bill_no=1, bill_date=date(2024, 2, 5),
        )
        self.assertEqual(len(bill["tranche_breakdown"]), 2)
        self.assertGreater(Decimal(bill["interest_amount"]), Decimal('0'))

    def test_tranche_released_after_period_excluded(self):
        tranches = [{"amount": Decimal('500000'), "release_date": date(2024, 2, 10)}]
        bill = generate_pre_emi_bill(
            tranches, Decimal('9.00'), date(2024, 1, 1), date(2024, 1, 31),
            bill_no=1, bill_date=date(2024, 2, 5),
        )
        self.assertEqual(len(bill["tranche_breakdown"]), 0)
        self.assertEqual(Decimal(bill["interest_amount"]), Decimal('0.00'))


class TestEMISchedule(unittest.TestCase):
    """Full EMI schedule, rate reset, and repayment: BR-19 through BR-21."""

    def test_compute_emi_basic(self):
        emi = compute_emi(Decimal('1000000'), Decimal('9.00'), 240)
        self.assertGreater(emi, Decimal('8900'))
        self.assertLess(emi, Decimal('9100'))

    def test_generate_schedule_length(self):
        sched = generate_schedule(Decimal('1000000'), Decimal('9.00'), 24, date(2024, 2, 1))
        self.assertEqual(len(sched), 24)

    def test_generate_schedule_outstanding_cleared(self):
        sched = generate_schedule(Decimal('500000'), Decimal('9.00'), 12, date(2024, 2, 1))
        self.assertEqual(Decimal(sched[-1]["outstanding_principal"]), Decimal('0'))

    def test_benchmark_change_effective_next_month(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.process_benchmark_change(date(2024, 6, 1), Decimal('9.00'))
        self.assertEqual(eng._rate_effective_date, date(2024, 7, 1))

    def test_rate_reset_exceeds_age_cap(self):
        # 68-year old borrower: only 24 months remaining till 70
        dob = date(1956, 1, 1)
        new_emi, new_tenure, _, reason = apply_rate_reset(
            outstanding=Decimal('500000'),
            old_rate=Decimal('8.50'),
            new_rate=Decimal('10.50'),
            current_emi=Decimal('10000'),
            remaining_tenure=60,
            reset_date=date(2024, 1, 1),
            borrower_dob=dob,
            next_due_date=date(2024, 2, 1),
        )
        self.assertLessEqual(new_tenure, 24)
        self.assertIn("age 70", reason)

    def test_rate_reset_within_age_cap(self):
        # Young borrower: keeps EMI constant, extends tenure
        dob = date(1995, 1, 1)
        new_emi, new_tenure, _, reason = apply_rate_reset(
            outstanding=Decimal('1000000'),
            old_rate=Decimal('8.50'),
            new_rate=Decimal('9.50'),
            current_emi=Decimal('15000'),
            remaining_tenure=85,
            reset_date=date(2024, 1, 1),
            borrower_dob=dob,
            next_due_date=date(2024, 2, 1),
        )
        self.assertEqual(new_emi, Decimal('15000'))
        self.assertGreater(new_tenure, 85)

    def test_rate_reset_insufficient_emi_increases_emi(self):
        # If EMI cannot cover even 1 month of interest at new rate -> must increase EMI
        dob = date(1995, 1, 1)
        new_emi, _, _, _ = apply_rate_reset(
            outstanding=Decimal('2000000'),
            old_rate=Decimal('5.00'),
            new_rate=Decimal('15.00'),  # 15% on 20L = 25,000/mo interest
            current_emi=Decimal('10000'), # less than monthly interest!
            remaining_tenure=120,
            reset_date=date(2024, 1, 1),
            borrower_dob=dob,
            next_due_date=date(2024, 2, 1),
        )
        self.assertGreater(new_emi, Decimal('25000'))

    def test_final_tranche_last_day_month36(self):
        loan = _make_dummy_loan()
        eng = LoanEngine(loan, _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        eng.process_stage_certified(date(2024, 1, 20), "Foundation", date(2024, 1, 20))
        eng.process_demand(date(2024, 1, 20), date(2024, 1, 20), Decimal('600000'), "Foundation", Decimal('20'))
        # Month 36 completed by 2027-02-05
        eng.run_monthly_job(date(2027, 2, 5))
        self.assertTrue(eng.converted)

    def test_next_emi_due_date_after_wrap(self):
        d = next_emi_due_date_after(date(2024, 12, 15), 15)
        self.assertEqual(d, date(2025, 1, 15))

    def test_next_emi_due_date_after_leap(self):
        # Jan 31 -> Feb 29 (in 2024 leap year)
        d = next_emi_due_date_after(date(2024, 1, 31), 31)
        self.assertEqual(d, date(2024, 2, 29))

    def test_recalculate_after_prepayment_reduce_tenure(self):
        sched = recalculate_schedule_after_prepayment(
            outstanding=Decimal('800000'),
            annual_rate_pct=Decimal('9.00'),
            remaining_tenure=120,
            next_due_date=date(2024, 2, 1),
            reduce_tenure=True,
            current_emi=Decimal('15000'),
        )
        self.assertLess(len(sched), 120)

    def test_recalculate_after_prepayment_reduce_emi(self):
        sched = recalculate_schedule_after_prepayment(
            outstanding=Decimal('800000'),
            annual_rate_pct=Decimal('9.00'),
            remaining_tenure=120,
            next_due_date=date(2024, 2, 1),
            reduce_tenure=False,
        )
        self.assertEqual(len(sched), 120)
        self.assertLess(Decimal(sched[0]["emi_amount"]), Decimal('15000'))

    def test_consecutive_missed_emis_empty(self):
        loan = _make_dummy_loan()
        self.assertEqual(consecutive_missed_emis(loan), 0)

    def test_consecutive_missed_emis_mixed_status(self):
        loan = _make_dummy_loan()
        loan.emi_schedule = [
            {"status": "PAID"},
            {"status": "OVERDUE"},
            {"status": "OVERDUE"},
        ]
        self.assertEqual(consecutive_missed_emis(loan), 2)


class TestEdgeCases(unittest.TestCase):
    """Edge cases specified in design docs and project spec."""

    def test_prepayment_on_tranche_release_date(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        eng.process_stage_certified(date(2024, 2, 1), "Foundation", date(2024, 2, 1))
        eng.process_demand(date(2024, 2, 1), date(2024, 2, 1), Decimal('600000'), "Foundation", Decimal('20'))
        eng._convert_to_full_emi(date(2024, 2, 1))
        # Prepay on same date
        log = eng.process_part_prepayment(date(2024, 2, 1), Decimal('100000'), reduce_tenure=True)
        self.assertEqual(log.outcome, "SUCCESS")

    def test_final_tranche_last_day_month36(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        eng.process_stage_certified(date(2024, 2, 1), "Finishing", date(2024, 2, 1))
        # Trigger conversion directly
        eng._convert_to_full_emi(date(2027, 1, 15))
        self.assertTrue(eng.converted)

    def test_exact_prepayment_50000(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng._convert_to_full_emi(date(2024, 2, 1))
        log = eng.process_part_prepayment(date(2024, 2, 15), Decimal('50000'), reduce_tenure=True)
        self.assertEqual(log.outcome, "SUCCESS")

    def test_prepayment_below_50000_rejected(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng._convert_to_full_emi(date(2024, 2, 1))
        log = eng.process_part_prepayment(date(2024, 2, 15), Decimal('49999'), reduce_tenure=True)
        self.assertEqual(log.outcome, "REJECTED")

    def test_full_prepayment_closes_account(self):
        loan = _make_dummy_loan()
        eng = LoanEngine(loan, _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng._convert_to_full_emi(date(2024, 2, 1))
        bal = eng._current_outstanding()
        log = eng.process_full_prepayment(date(2024, 3, 1), bal)
        self.assertEqual(log.outcome, "SUCCESS")
        self.assertEqual(eng.loan.status, "CLOSED")

    def test_full_prepayment_insufficient_rejected(self):
        eng = LoanEngine(_make_dummy_loan(), _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.total_disbursed = Decimal('1000000')
        eng._convert_to_full_emi(date(2024, 2, 1))
        log = eng.process_full_prepayment(date(2024, 3, 1), Decimal('1000'))
        self.assertEqual(log.outcome, "REJECTED")

    def test_month36_lapse_undisbursed(self):
        loan = _make_dummy_loan(sanctioned_amount=Decimal('3000000'))
        loan.disbursement_records = [
            {"amount": Decimal('1000000'), "decision": "RELEASED"}
        ]
        eng = LoanEngine(loan, _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        # Advance to month 37
        eng.run_monthly_job(date(2027, 2, 5))
        self.assertTrue(eng.converted)

    def test_conversion_on_month37_if_not_fully_disbursed(self):
        loan = _make_dummy_loan()
        eng = LoanEngine(loan, _make_dummy_borrower(), _make_dummy_property(), Decimal('8.50'))
        eng.run_monthly_job(date(2027, 3, 5))
        self.assertTrue(eng.converted)


class TestLoanEngineIntegration(unittest.TestCase):
    """Full lifecycle engine integration tests."""

    def setUp(self):
        self.loan = _make_dummy_loan()
        self.borrower = _make_dummy_borrower()
        self.property = _make_dummy_property()
        self.eng = LoanEngine(self.loan, self.borrower, self.property, Decimal('8.50'))

    def test_engine_initialization(self):
        self.assertEqual(self.eng.loan.status, "SANCTIONED")
        self.assertFalse(self.eng.converted)

    def test_engine_own_contribution_recording(self):
        log = self.eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        self.assertTrue(self.eng.own_contribution_paid)
        self.assertEqual(log.outcome, "SUCCESS")

    def test_engine_stage_certification(self):
        log = self.eng.process_stage_certified(date(2024, 2, 1), "Foundation", date(2024, 2, 1))
        self.assertEqual(self.eng.cumulative_certified_pct, Decimal('20'))
        self.assertEqual(log.outcome, "INFO")

    def test_engine_demand_and_release(self):
        self.eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        self.eng.process_stage_certified(date(2024, 2, 1), "Foundation", date(2024, 2, 1))
        rec = self.eng.process_demand(date(2024, 2, 10), date(2024, 2, 10), Decimal('600000'), "Foundation", Decimal('20'))
        self.assertEqual(rec.decision, "RELEASED")
        self.assertEqual(rec.amount, Decimal('600000'))

    def test_engine_demand_rejection(self):
        # Demand without own contribution
        rec = self.eng.process_demand(date(2024, 2, 10), date(2024, 2, 10), Decimal('600000'), "Foundation", Decimal('20'))
        self.assertEqual(rec.decision, "REJECTED")

    def test_engine_monthly_job_pre_emi(self):
        self.eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        self.eng.process_stage_certified(date(2024, 2, 1), "Foundation", date(2024, 2, 1))
        self.eng.process_demand(date(2024, 2, 10), date(2024, 2, 10), Decimal('600000'), "Foundation", Decimal('20'))
        self.eng.run_monthly_job(date(2024, 3, 5))
        self.assertEqual(len(self.eng.loan.pre_emi_bills), 1)

    def test_engine_benchmark_change(self):
        self.eng.process_benchmark_change(date(2024, 6, 1), Decimal('9.00'))
        self.assertIsNotNone(self.eng._pending_rate)

    def test_engine_conversion_on_final_stage(self):
        self.eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        # Certify all stages (100%)
        self.eng.process_stage_certified(date(2024, 2, 1), "Finishing", date(2024, 2, 1))
        self.eng.cumulative_certified_pct = Decimal('100')
        self.eng.process_demand(date(2024, 2, 10), date(2024, 2, 10), Decimal('2700000'), "Finishing", Decimal('100'))
        self.assertTrue(self.eng.converted)
        self.assertEqual(self.eng.loan.status, "ACTIVE_EMI")

    def test_engine_emi_payment_recording(self):
        self.eng._convert_to_full_emi(date(2024, 2, 1))
        log = self.eng.record_emi_payment(date(2024, 3, 1), emi_no=1, amount=Decimal('30000'))
        self.assertEqual(log.outcome, "SUCCESS")

    def test_engine_part_prepayment(self):
        self.eng._convert_to_full_emi(date(2024, 2, 1))
        log = self.eng.process_part_prepayment(date(2024, 3, 15), Decimal('100000'), reduce_tenure=True)
        self.assertEqual(log.outcome, "SUCCESS")

    def test_engine_full_prepayment(self):
        self.eng._convert_to_full_emi(date(2024, 2, 1))
        bal = self.eng._current_outstanding()
        log = self.eng.process_full_prepayment(date(2024, 4, 1), bal)
        self.assertEqual(self.eng.loan.status, "CLOSED")

    def test_engine_event_log_sequence(self):
        self.eng.process_own_contribution(date(2024, 1, 20), Decimal('300000'))
        self.eng.process_stage_certified(date(2024, 2, 1), "Foundation", date(2024, 2, 1))
        self.assertGreaterEqual(len(self.eng.event_log), 2)
        self.assertEqual(self.eng.event_log[0].event_no, 1)
        self.assertEqual(self.eng.event_log[1].event_no, 2)


if __name__ == "__main__":
    unittest.main()
