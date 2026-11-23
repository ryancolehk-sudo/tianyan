"""Financial presentation edge cases runnable without a database or network."""
import unittest

from app.market.quote_values import changes


class QuoteValueTests(unittest.TestCase):
    def test_decimal_price_difference_and_percentage_points(self):
        self.assertEqual(changes('12.30','12'),('0.30','2.50'))
        self.assertEqual(changes('0.11','0.10'),('0.01','10.00'))
        self.assertEqual(changes('12','16'),('-4','-25.00'))

    def test_zero_or_unknown_previous_close_is_not_a_zero_return(self):
        for price,previous in [('12',None),(None,'12'),('12','0'),('0','12')]:
            with self.subTest(price=price,previous=previous):
                self.assertEqual(changes(price,previous),(None,None))

    def test_non_finite_or_negative_values_cannot_create_a_financial_number(self):
        for price in ['NaN','Infinity','-1','not-a-number']:
            self.assertEqual(changes(price,'12'),(None,None))

    def test_large_decimal_amounts_do_not_lose_low_order_digits(self):
        self.assertEqual(changes('9999999999999999.12345678','9999999999999999.12345677'),('0.00000001','0.00'))

    def test_tiny_decline_does_not_show_negative_zero_percentage(self):
        self.assertEqual(changes('99.9999','100'),('-0.0001','0.00'))
