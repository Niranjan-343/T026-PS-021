"""
tests/test_phase1.py — Phase 1 Unit Tests (BR-01 to BR-10)
Team T026 / PS-021
Total: 46 unit tests.
"""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from src.models.borrower import Borrower, CoApplicant
from src.models.property import Property, StageItem
from src.rules.business_rules import BusinessRules
from src.calculations.eligibility import (
    calculate_combined_income,
    calculate_ltv_limit,
    calculate_max_tenure,
    calculate_interest_rate,
    calculate_eligibility_limit,
)
from src.calculations.fees import calculate_processing_fee
from src.calculations.sanction import process_sanction, _add_months


class TestLTV(unittest.TestCase):
    """BR-01: LTV ratios and boundaries."""

    def test_ltv_below_30_lakh(self):
        ratio = BusinessRules.ltv_ratio(Decimal('2500000'))
        self.assertEqual(ratio, Decimal('0.90'))

    def test_ltv_30_lakh_boundary(self):
        ratio = BusinessRules.ltv_ratio(Decimal('3000000'))
        self.assertEqual(ratio, Decimal('0.90'))

    def test_ltv_between_30_and_75_lakh(self):
        ratio = BusinessRules.ltv_ratio(Decimal('5000000'))
        self.assertEqual(ratio, Decimal('0.80'))

    def test_ltv_75_lakh_boundary(self):
        ratio = BusinessRules.ltv_ratio(Decimal('7500000'))
        self.assertEqual(ratio, Decimal('0.80'))

    def test_ltv_above_75_lakh(self):
        ratio = BusinessRules.ltv_ratio(Decimal('10000000'))
        self.assertEqual(ratio, Decimal('0.75'))

    def test_ltv_zero(self):
        ratio = BusinessRules.ltv_ratio(Decimal('0'))
        self.assertEqual(ratio, Decimal('0.90'))

    def test_ltv_rule_id(self):
        amt, rule_id = calculate_ltv_limit(Decimal('5000000'))
        self.assertEqual(rule_id, "BR-01")
        self.assertEqual(amt, Decimal('4000000'))

    def test_ltv_rounding(self):
        amt, _ = calculate_ltv_limit(Decimal('3333333.33'))
        self.assertEqual(amt, Decimal('2666667'))


class TestTenure(unittest.TestCase):
    """BR-02: Maximum eligible tenure."""

    def test_tenure_young_borrower(self):
        # Age 25: 70 - 25 = 45 years -> capped at 30 years (360 months)
        self.assertEqual(BusinessRules.max_tenure_months(25), 360)

    def test_tenure_age_40(self):
        # Age 40: 70 - 40 = 30 years -> 360 months
        self.assertEqual(BusinessRules.max_tenure_months(40), 360)

    def test_tenure_age_55(self):
        # Age 55: 70 - 55 = 15 years -> 180 months
        self.assertEqual(BusinessRules.max_tenure_months(55), 180)

    def test_tenure_age_65(self):
        # Age 65: 70 - 65 = 5 years -> 60 months
        self.assertEqual(BusinessRules.max_tenure_months(65), 60)

    def test_tenure_cap_30_years(self):
        b = Borrower("B99", "Young", date(2000, 1, 1), "M", Decimal('50000'), 750)
        tenure, rule_id = calculate_max_tenure(b, date(2024, 1, 1))
        self.assertEqual(tenure, 360)
        self.assertEqual(rule_id, "BR-02")


class TestCombinedIncome(unittest.TestCase):
    """BR-03 / BR-04: Co-applicant weighting."""

    def test_single_applicant(self):
        b = Borrower("B1", "A", date(1990, 1, 1), "M", Decimal('100000'), 750)
        self.assertEqual(calculate_combined_income(b), Decimal('100000'))

    def test_coapplicant_spouse(self):
        coa = CoApplicant("Spouse", Decimal('50000'), "SPOUSE")
        b = Borrower("B1", "A", date(1990, 1, 1), "M", Decimal('100000'), 750, coa)
        self.assertEqual(calculate_combined_income(b), Decimal('150000'))

    def test_coapplicant_other(self):
        coa = CoApplicant("Parent", Decimal('50000'), "OTHER")
        b = Borrower("B1", "A", date(1990, 1, 1), "M", Decimal('100000'), 750, coa)
        self.assertEqual(calculate_combined_income(b), Decimal('125000.0'))

    def test_coapplicant_zero_income(self):
        coa = CoApplicant("Spouse", Decimal('0'), "SPOUSE")
        b = Borrower("B1", "A", date(1990, 1, 1), "M", Decimal('80000'), 750, coa)
        self.assertEqual(calculate_combined_income(b), Decimal('80000'))

    def test_invalid_coapplicant_relation(self):
        with self.assertRaises(ValueError):
            CoApplicant("Friend", Decimal('50000'), "FRIEND")


class TestInterestRate(unittest.TestCase):
    """BR-05 / BR-06: Credit score spread and women concession."""

    def test_high_credit_score_male(self):
        rate, rules = calculate_interest_rate(Decimal('8.50'), 800, "M")
        self.assertEqual(rate, Decimal('9.00'))
        self.assertIn("BR-05", rules)
        self.assertNotIn("BR-06", rules)

    def test_high_credit_score_female(self):
        rate, rules = calculate_interest_rate(Decimal('8.50'), 800, "F")
        self.assertEqual(rate, Decimal('8.95'))
        self.assertIn("BR-05", rules)
        self.assertIn("BR-06", rules)

    def test_mid_credit_score_male(self):
        rate, _ = calculate_interest_rate(Decimal('8.50'), 750, "M")
        self.assertEqual(rate, Decimal('9.40'))

    def test_mid_credit_score_female(self):
        rate, _ = calculate_interest_rate(Decimal('8.50'), 750, "F")
        self.assertEqual(rate, Decimal('9.35'))

    def test_low_credit_score_male(self):
        rate, _ = calculate_interest_rate(Decimal('8.50'), 700, "M")
        self.assertEqual(rate, Decimal('9.90'))

    def test_low_credit_score_female(self):
        rate, _ = calculate_interest_rate(Decimal('8.50'), 700, "F")
        self.assertEqual(rate, Decimal('9.85'))

    def test_boundary_775(self):
        # 775 gets 0.50% spread
        rate, _ = calculate_interest_rate(Decimal('8.50'), 775, "M")
        self.assertEqual(rate, Decimal('9.00'))

    def test_boundary_725(self):
        # 725 gets 0.90% spread
        rate, _ = calculate_interest_rate(Decimal('8.50'), 725, "M")
        self.assertEqual(rate, Decimal('9.40'))


class TestFees(unittest.TestCase):
    """BR-08: Processing fee and GST."""

    def test_fee_minimum_cap(self):
        fee = calculate_processing_fee(Decimal('1000000'))
        self.assertEqual(fee["gross_fee"], Decimal('10000.00'))

    def test_fee_maximum_cap(self):
        fee = calculate_processing_fee(Decimal('10000000'))
        self.assertEqual(fee["gross_fee"], Decimal('25000.00'))

    def test_fee_proportional(self):
        fee = calculate_processing_fee(Decimal('3000000'))
        self.assertEqual(fee["gross_fee"], Decimal('15000.00'))

    def test_gst_calculation(self):
        fee = calculate_processing_fee(Decimal('3000000'))
        self.assertEqual(fee["gst"], Decimal('2700.00'))
        self.assertEqual(fee["total_fee"], Decimal('17700.00'))

    def test_fee_tranche_split(self):
        fee = calculate_processing_fee(Decimal('4000000'))
        self.assertEqual(fee["at_sanction"] + fee["at_first_disbursal"], fee["total_fee"])

    def test_fee_round_trip(self):
        fee = calculate_processing_fee(Decimal('2500000'))
        self.assertEqual(fee["gross_fee"], Decimal('12500.00'))
        self.assertEqual(fee["gst"], Decimal('2250.00'))


class TestEligibilityAndSanction(unittest.TestCase):
    """BR-03, BR-07, BR-09, BR-10: EMI ratios, eligibility and sanction."""

    def test_emi_ratio_low_income(self):
        self.assertEqual(BusinessRules.emi_income_ratio(Decimal('90000')), Decimal('0.55'))

    def test_emi_ratio_high_income(self):
        self.assertEqual(BusinessRules.emi_income_ratio(Decimal('150000')), Decimal('0.60'))

    def test_pmt_formula(self):
        emi = BusinessRules.emi_amount(Decimal('1000000'), Decimal('9.00'), 240)
        self.assertGreater(emi, Decimal('8900'))
        self.assertLess(emi, Decimal('9100'))

    def test_sanction_governed_by_ltv(self):
        # LTV = 27L, Eligibility = 50L -> Sanction = 27L
        self.assertEqual(BusinessRules.sanctioned_amount(Decimal('2700000'), Decimal('5000000')), Decimal('2700000'))

    def test_sanction_governed_by_eligibility(self):
        # LTV = 40L, Eligibility = 35L -> Sanction = 35L
        self.assertEqual(BusinessRules.sanctioned_amount(Decimal('4000000'), Decimal('3500000')), Decimal('3500000'))

    def test_sanction_validity_6_months(self):
        start = date(2024, 1, 15)
        expiry = _add_months(start, 6)
        self.assertEqual(expiry, date(2024, 7, 15))

    def test_sanction_validity_month_end_clamp(self):
        start = date(2023, 8, 31)
        expiry = _add_months(start, 6)
        self.assertEqual(expiry, date(2024, 2, 29))  # 2024 is leap year

    def test_full_process_sanction(self):
        b = Borrower("B1", "Meera", date(1990, 6, 20), "F", Decimal('150000'), 780)
        p = Property("P1", Decimal('3000000'), "Prestige", [
            StageItem("Foundation", Decimal('50')),
            StageItem("Finishing", Decimal('50')),
        ])
        result = process_sanction(b, p, Decimal('8.50'), date(2024, 1, 15))
        self.assertEqual(result.sanctioned_amount, Decimal('2700000'))
        self.assertEqual(result.status, "SANCTIONED")
        self.assertIn("BR-01", result.rule_ids_applied)
        self.assertIn("BR-07", result.rule_ids_applied)


class TestModelValidation(unittest.TestCase):
    """Domain model dataclass validation."""

    def test_borrower_valid_gender(self):
        b = Borrower("B1", "A", date(1990, 1, 1), "M", Decimal('50000'), 700)
        self.assertEqual(b.gender, "M")

    def test_borrower_invalid_gender(self):
        with self.assertRaises(ValueError):
            Borrower("B1", "A", date(1990, 1, 1), "X", Decimal('50000'), 700)

    def test_borrower_age_calculation(self):
        b = Borrower("B1", "A", date(1990, 6, 15), "M", Decimal('50000'), 700)
        self.assertEqual(b.age_at_date(date(2024, 6, 14)), 33)
        self.assertEqual(b.age_at_date(date(2024, 6, 15)), 34)

    def test_property_stages_sum_100(self):
        p = Property("P1", Decimal('3000000'), "Builder", [
            StageItem("S1", Decimal('40')),
            StageItem("S2", Decimal('60')),
        ])
        self.assertTrue(p.validate_stages())

    def test_property_stages_not_100(self):
        p = Property("P1", Decimal('3000000'), "Builder", [
            StageItem("S1", Decimal('40')),
            StageItem("S2", Decimal('50')),
        ])
        self.assertFalse(p.validate_stages())

    def test_coapplicant_invalid_relationship(self):
        with self.assertRaises(ValueError):
            CoApplicant("Co", Decimal('10000'), "COUSIN")


if __name__ == "__main__":
    unittest.main()
