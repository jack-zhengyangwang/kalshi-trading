"""
tests/test_tradetape.py — Trade-tape fill math tests.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import wc.tradetape as tt

# Synthetic minute-book: {minute_offset: {price, vol}}
BOOK = {
    -5: {"price": 0.50, "vol": 10.0},
    0:  {"price": 0.52, "vol": 5.0},
    5:  {"price": 0.55, "vol": 8.0},
    10: {"price": 0.53, "vol": 3.0},
}


class TestPriceAt(unittest.TestCase):
    def test_price_at_exact(self):
        self.assertEqual(tt.price_at(BOOK, 5), 0.55)

    def test_price_at_between(self):
        self.assertEqual(tt.price_at(BOOK, 7), 0.55)

    def test_price_at_pre_game(self):
        self.assertEqual(tt.price_at(BOOK, -1), 0.50)

    def test_price_at_before_any(self):
        self.assertIsNone(tt.price_at(BOOK, -10))

    def test_price_at_empty_book(self):
        self.assertIsNone(tt.price_at({}, 0))


class TestFillable(unittest.TestCase):
    def test_fillable_default_window(self):
        self.assertEqual(tt.fillable(BOOK, 5, window=10), 23.0)

    def test_fillable_narrow_window(self):
        self.assertEqual(tt.fillable(BOOK, 10, window=3), 3.0)

    def test_fillable_no_overlap(self):
        self.assertEqual(tt.fillable(BOOK, -20, window=5), 0.0)


class TestFill(unittest.TestCase):
    def test_fill_fully_met(self):
        filled, status = tt.fill(10, BOOK, 5, window=10)
        self.assertEqual(filled, 10)
        self.assertEqual(status, "met")

    def test_fill_partial(self):
        filled, status = tt.fill(50, BOOK, 5, window=10)
        self.assertEqual(filled, 23)
        self.assertEqual(status, "partial")

    def test_fill_unmet(self):
        filled, status = tt.fill(10, BOOK, -20, window=5)
        self.assertEqual(filled, 0)
        self.assertEqual(status, "unmet")
