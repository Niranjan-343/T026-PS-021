"""
tests/test_engine.py
Unit and integration tests for T026-PS-021 Mortgage Servicing Engine.
Tests the run_sample.py engine against expected output_sample data.
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "official_sample"
EXPECTED_DIR = PROJECT_ROOT / "output_sample"
TEST_OUTPUT_DIR = PROJECT_ROOT / "output_test_suite"

# ── Import functions from run_sample.py ────────────────────────────────────
sys.path.insert(0, str(PROJECT_ROOT))
from run_sample import (
    compute_ltv,
    compute_spread,
    pmt,
    max_loan_by_income,
    add_months,
    month_start,
    month_end,
    months_between,
    D,
    ZERO,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests — Core Financial Calculations
# ═══════════════════════════════════════════════════════════════════════════════

class TestLTV(unittest.TestCase):
    """BR-01: LTV ratio by agreement value slab."""

    def test_below_30_lakh(self):
        pct, amt = compute_ltv(D("2000000"))
        self.assertEqual(pct, D("90"))
        self.assertEqual(amt, D("1800000.00"))

    def test_exactly_30_lakh(self):
        pct, amt = compute_ltv(D("3000000"))
        self.assertEqual(pct, D("90"))
        self.assertEqual(amt, D("2700000.00"))

    def test_between_30_and_75_lakh(self):
        pct, amt = compute_ltv(D("5000000"))
        self.assertEqual(pct, D("80"))
        self.assertEqual(amt, D("4000000.00"))

    def test_exactly_75_lakh(self):
        pct, amt = compute_ltv(D("7500000"))
        self.assertEqual(pct, D("80"))
        self.assertEqual(amt, D("6000000.00"))

    def test_above_75_lakh(self):
        pct, amt = compute_ltv(D("10000000"))
        self.assertEqual(pct, D("75"))
        self.assertEqual(amt, D("7500000.00"))


class TestSpread(unittest.TestCase):
    """BR-05: Credit-score based spread."""

    def test_score_above_775(self):
        self.assertEqual(compute_spread(800), D("0.50"))

    def test_score_exactly_775(self):
        self.assertEqual(compute_spread(775), D("0.50"))

    def test_score_between_725_and_774(self):
        self.assertEqual(compute_spread(750), D("0.90"))

    def test_score_exactly_725(self):
        self.assertEqual(compute_spread(725), D("0.90"))

    def test_score_below_725(self):
        self.assertEqual(compute_spread(700), D("1.40"))


class TestPMT(unittest.TestCase):
    """EMI computation using PMT formula."""

    def test_basic_emi(self):
        # 10 lakh, 9%, 240 months => well-known EMI approx 8997
        emi = pmt(D("1000000"), D("9.00"), 240)
        self.assertGreater(emi, D("8900"))
        self.assertLess(emi, D("9100"))

    def test_zero_principal(self):
        self.assertEqual(pmt(ZERO, D("9.00"), 240), ZERO)

    def test_zero_tenure(self):
        self.assertEqual(pmt(D("1000000"), D("9.00"), 0), ZERO)

    def test_zero_rate(self):
        emi = pmt(D("1200000"), D("0"), 12)
        self.assertEqual(emi, D("100000"))


class TestMaxLoanByIncome(unittest.TestCase):
    """BR-03 + BR-10: Eligibility limit via income-based EMI cap."""

    def test_income_below_1_lakh(self):
        # 55% cap
        loan = max_loan_by_income(D("70000"), D("9.00"), 240)
        self.assertGreater(loan, D("0"))

    def test_income_above_1_lakh(self):
        # 60% cap
        loan = max_loan_by_income(D("150000"), D("9.00"), 240)
        self.assertGreater(loan, D("0"))

    def test_higher_income_gives_higher_eligibility(self):
        low = max_loan_by_income(D("50000"), D("9.00"), 240)
        high = max_loan_by_income(D("150000"), D("9.00"), 240)
        self.assertGreater(high, low)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests — Date Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestDateHelpers(unittest.TestCase):

    def test_add_months_normal(self):
        from datetime import date
        self.assertEqual(add_months(date(2024, 1, 15), 3), date(2024, 4, 15))

    def test_add_months_year_wrap(self):
        from datetime import date
        self.assertEqual(add_months(date(2024, 11, 10), 3), date(2025, 2, 10))

    def test_add_months_clamp_end_of_month(self):
        from datetime import date
        # Jan 31 + 1 month = Feb 28 (non-leap)
        self.assertEqual(add_months(date(2023, 1, 31), 1), date(2023, 2, 28))

    def test_add_months_leap_year(self):
        from datetime import date
        # Jan 31 + 1 month in leap year = Feb 29
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_month_start(self):
        from datetime import date
        self.assertEqual(month_start(date(2024, 3, 15)), date(2024, 3, 1))

    def test_month_end(self):
        from datetime import date
        self.assertEqual(month_end(date(2024, 2, 1)), date(2024, 2, 29))

    def test_months_between(self):
        from datetime import date
        self.assertEqual(months_between(date(2024, 1, 1), date(2024, 6, 1)), 5)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test — Full Engine Run vs Expected Outputs
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullEngineRun(unittest.TestCase):
    """
    Run the full engine with official sample data and compare outputs
    row-for-row against the expected output_sample/ files.
    """

    @classmethod
    def setUpClass(cls):
        """Run the engine once before all tests in this class."""
        if TEST_OUTPUT_DIR.exists():
            shutil.rmtree(TEST_OUTPUT_DIR)
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "run_sample.py"),
             str(DATA_DIR), "--output", str(TEST_OUTPUT_DIR)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        cls.engine_returncode = result.returncode
        cls.engine_stdout = result.stdout
        cls.engine_stderr = result.stderr

    @classmethod
    def tearDownClass(cls):
        """Clean up test output directory."""
        if TEST_OUTPUT_DIR.exists():
            shutil.rmtree(TEST_OUTPUT_DIR)

    def test_engine_exits_zero(self):
        self.assertEqual(self.engine_returncode, 0,
                         f"Engine failed:\nstdout: {self.engine_stdout}\nstderr: {self.engine_stderr}")

    def _compare_csv(self, filename: str):
        expected_path = EXPECTED_DIR / filename
        actual_path = TEST_OUTPUT_DIR / filename

        self.assertTrue(expected_path.exists(), f"Expected file missing: {expected_path}")
        self.assertTrue(actual_path.exists(), f"Output file not created: {actual_path}")

        with open(expected_path, newline="", encoding="utf-8") as f:
            expected = list(csv.reader(f))
        with open(actual_path, newline="", encoding="utf-8") as f:
            actual = list(csv.reader(f))

        self.assertEqual(len(expected), len(actual),
                         f"{filename}: row count mismatch (expected {len(expected)}, got {len(actual)})")

        for i, (exp_row, act_row) in enumerate(zip(expected, actual)):
            self.assertEqual(exp_row, act_row,
                             f"{filename} row {i} mismatch:\n  expected: {exp_row}\n  actual:   {act_row}")

    def test_sanction_letters_match(self):
        self._compare_csv("sanction_letters.csv")

    def test_tranche_decisions_match(self):
        self._compare_csv("tranche_decisions.csv")

    def test_pre_emi_bills_match(self):
        self._compare_csv("pre_emi_bills.csv")

    def test_schedule_match(self):
        self._compare_csv("schedule.csv")

    def test_interest_certificates_match(self):
        self._compare_csv("interest_certificates.csv")


# ═══════════════════════════════════════════════════════════════════════════════
# Output Validation Tests — Business Rule Checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestSanctionLetterValidation(unittest.TestCase):
    """Validate sanction letter contents against business rules."""

    @classmethod
    def setUpClass(cls):
        path = EXPECTED_DIR / "sanction_letters.csv"
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.sanctions = list(reader)

    def test_all_sanctions_have_loan_id(self):
        for s in self.sanctions:
            self.assertTrue(s.get("loan_id", "").startswith("HL-"),
                            f"Invalid loan_id: {s.get('loan_id')}")

    def test_all_sanctions_approved(self):
        for s in self.sanctions:
            self.assertEqual(s["decision"], "APPROVED",
                             f"{s['loan_id']}: decision {s['decision']} != APPROVED")

    def test_sanctioned_amount_within_ltv(self):
        """BR-07: Sanction ≤ LTV limit."""
        for s in self.sanctions:
            sanctioned = Decimal(s["sanctioned_amount"])
            ltv_amount = Decimal(s["ltv_amount"])
            self.assertLessEqual(sanctioned, ltv_amount + Decimal("0.01"),
                                 f"{s['loan_id']}: sanctioned {sanctioned} > LTV {ltv_amount}")

    def test_sanctioned_amount_within_eligibility(self):
        """BR-07: Sanction ≤ eligibility limit."""
        for s in self.sanctions:
            sanctioned = Decimal(s["sanctioned_amount"])
            eligibility = Decimal(s["eligibility_amount"])
            self.assertLessEqual(sanctioned, eligibility + Decimal("0.01"),
                                 f"{s['loan_id']}: sanctioned {sanctioned} > eligibility {eligibility}")

    def test_processing_fee_bounds(self):
        """BR-08: Processing fee 0.5% of sanction, min ₹10,000, max ₹25,000."""
        for s in self.sanctions:
            fee = Decimal(s["processing_fee"])
            self.assertGreaterEqual(fee, Decimal("10000"),
                                    f"{s['loan_id']}: fee {fee} < ₹10,000")
            self.assertLessEqual(fee, Decimal("25000"),
                                 f"{s['loan_id']}: fee {fee} > ₹25,000")

    def test_gst_is_18_percent_of_fee(self):
        """BR-08: GST = 18% of processing fee."""
        for s in self.sanctions:
            fee = Decimal(s["processing_fee"])
            gst = Decimal(s["fee_gst"])
            expected_gst = (fee * Decimal("0.18")).quantize(Decimal("0.01"), ROUND_HALF_UP)
            self.assertEqual(gst, expected_gst,
                             f"{s['loan_id']}: GST {gst} != 18% of fee {fee} = {expected_gst}")


class TestTrancheDecisionValidation(unittest.TestCase):
    """Validate tranche decisions against business rules."""

    @classmethod
    def setUpClass(cls):
        path = EXPECTED_DIR / "tranche_decisions.csv"
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.tranches = list(reader)

    def test_valid_decisions(self):
        valid = {"RELEASED", "REJECTED", "PARTIAL", "FROZEN"}
        for t in self.tranches:
            self.assertIn(t["decision"], valid,
                          f"Unexpected decision: {t['decision']}")

    def test_released_amount_not_negative(self):
        for t in self.tranches:
            amt = Decimal(t["released_amount"])
            self.assertGreaterEqual(amt, ZERO,
                                    f"{t['demand_id']}: negative released amount {amt}")


class TestPreEmiBillValidation(unittest.TestCase):
    """Validate pre-EMI bills against BR-17."""

    @classmethod
    def setUpClass(cls):
        path = EXPECTED_DIR / "pre_emi_bills.csv"
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.bills = list(reader)

    def test_interest_positive(self):
        for b in self.bills:
            interest = Decimal(b["interest"])
            self.assertGreater(interest, ZERO,
                               f"Pre-EMI interest should be > 0, got {interest}")

    def test_period_ordering(self):
        from datetime import date
        for b in self.bills:
            period_from = date.fromisoformat(b["period_from"])
            period_to = date.fromisoformat(b["period_to"])
            self.assertLess(period_from, period_to,
                            f"period_from {period_from} >= period_to {period_to}")


if __name__ == "__main__":
    unittest.main()
