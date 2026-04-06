"""Tests for core/risk/correlation_manager.py"""

import unittest

from core.risk.correlation_manager import CorrelationManager, CorrelationResult
from config import constants as C


class TestCorrelationManager(unittest.TestCase):

    def setUp(self):
        self.cm = CorrelationManager()

    # ------------------------------------------------------------------
    # add / remove / update
    # ------------------------------------------------------------------

    def test_add_position_increases_count(self):
        self.cm.add_position("T1", "economics", "buy_yes", 25.0)
        self.assertEqual(self.cm.open_count(), 1)

    def test_remove_position_decreases_count(self):
        self.cm.add_position("T1", "economics", "buy_yes", 25.0)
        self.cm.remove_position("T1")
        self.assertEqual(self.cm.open_count(), 0)

    def test_remove_nonexistent_returns_false(self):
        self.assertFalse(self.cm.remove_position("MISSING"))

    def test_total_exposure_sums_all_positions(self):
        self.cm.add_position("T1", "economics", "buy_yes", 25.0)
        self.cm.add_position("T2", "politics",  "buy_no",  30.0)
        self.assertAlmostEqual(self.cm.total_exposure(), 55.0)

    def test_update_size_changes_dollar_size(self):
        self.cm.add_position("T1", "economics", "buy_yes", 25.0)
        self.cm.update_size("T1", 40.0)
        self.assertAlmostEqual(self.cm.total_exposure(), 40.0)

    def test_tickers_returns_all_open(self):
        self.cm.add_position("T1", "economics", "buy_yes", 10.0)
        self.cm.add_position("T2", "politics",  "buy_no",  10.0)
        self.assertIn("T1", self.cm.tickers())
        self.assertIn("T2", self.cm.tickers())

    def test_exposure_by_category(self):
        self.cm.add_position("T1", "economics", "buy_yes", 20.0)
        self.cm.add_position("T2", "economics", "buy_no",  15.0)
        self.cm.add_position("T3", "politics",  "buy_yes", 10.0)
        by_cat = self.cm.exposure_by_category()
        self.assertAlmostEqual(by_cat["economics"], 35.0)
        self.assertAlmostEqual(by_cat["politics"],  10.0)

    def test_exposure_by_bucket(self):
        self.cm.add_position("T1", "economics", "buy_yes", 20.0)
        self.cm.add_position("T2", "economics", "buy_yes", 10.0)
        by_bucket = self.cm.exposure_by_bucket()
        self.assertAlmostEqual(by_bucket[("economics", "buy_yes")], 30.0)

    # ------------------------------------------------------------------
    # check — allowed
    # ------------------------------------------------------------------

    def test_check_allows_first_position(self):
        result = self.cm.check("economics", "buy_yes", 25.0, 500.0)
        self.assertTrue(result.allowed)

    def test_check_returns_correlation_result(self):
        result = self.cm.check("economics", "buy_yes", 25.0, 500.0)
        self.assertIsInstance(result, CorrelationResult)

    def test_check_allows_up_to_max_positions(self):
        # Add 4 positions first
        for i in range(C.MAX_OPEN_POSITIONS - 1):
            self.cm.add_position(f"T{i}", "economics", "buy_yes", 5.0)
            result = self.cm.check("politics", "buy_no", 5.0, 500.0)
        self.assertTrue(result.allowed)

    # ------------------------------------------------------------------
    # check — blocked
    # ------------------------------------------------------------------

    def test_blocks_when_max_positions_reached(self):
        for i in range(C.MAX_OPEN_POSITIONS):
            self.cm.add_position(f"T{i}", f"cat{i}", "buy_yes", 5.0)
        result = self.cm.check("economics", "buy_yes", 5.0, 500.0)
        self.assertFalse(result.allowed)
        self.assertIn("max_open_positions", result.reason)

    def test_blocks_when_total_exposure_exceeds_50pct(self):
        # Already 45% of 500 = $225 deployed
        self.cm.add_position("T1", "economics", "buy_yes", 225.0)
        # Trying to add $30 more → 255/500 = 51% > 50%
        result = self.cm.check("politics", "buy_no", 30.0, 500.0)
        self.assertFalse(result.allowed)
        self.assertIn("total_exposure", result.reason)

    def test_blocks_when_correlated_exposure_exceeds_30pct(self):
        # 30% of 500 = $150; put $140 in economics/buy_yes
        self.cm.add_position("T1", "economics", "buy_yes", 140.0)
        # Try to add $20 more → 160/500 = 32% > 30%
        result = self.cm.check("economics", "buy_yes", 20.0, 500.0)
        self.assertFalse(result.allowed)
        self.assertIn("correlated_exposure", result.reason)

    def test_different_direction_not_correlated(self):
        # $140 in economics/buy_yes
        self.cm.add_position("T1", "economics", "buy_yes", 140.0)
        # economics/buy_NO is a DIFFERENT bucket
        result = self.cm.check("economics", "buy_no", 20.0, 500.0)
        self.assertTrue(result.allowed)

    def test_different_category_not_correlated(self):
        # $140 in economics/buy_yes
        self.cm.add_position("T1", "economics", "buy_yes", 140.0)
        # politics/buy_yes — different category
        result = self.cm.check("politics", "buy_yes", 20.0, 500.0)
        self.assertTrue(result.allowed)

    # ------------------------------------------------------------------
    # Boundary / edge cases
    # ------------------------------------------------------------------

    def test_exposure_pct_reported_correctly(self):
        self.cm.add_position("T1", "economics", "buy_yes", 50.0)
        result = self.cm.check("politics", "buy_no", 50.0, 500.0)
        # After adding: total = 100, exposure_pct = 100/500 = 20%
        self.assertAlmostEqual(result.exposure_pct, 0.10, places=2)  # current before add

    def test_category_normalised_lowercase(self):
        self.cm.add_position("T1", "Economics", "buy_yes", 140.0)
        # Should recognise same category
        result = self.cm.check("economics", "buy_yes", 20.0, 500.0)
        self.assertFalse(result.allowed)  # correlated limit hit

    def test_empty_manager_allows_any_position(self):
        result = self.cm.check("tech", "buy_yes", 100.0, 500.0)
        self.assertTrue(result.allowed)


if __name__ == "__main__":
    unittest.main()
