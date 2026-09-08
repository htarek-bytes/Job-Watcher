"""Pay, read out of prose.

There is no structured salary field on any of these APIs, so this is a parser
over free text and the interesting cases are all the ways free text lies. Two
rules matter more than the rest and both have their own class below: silence
is not zero, and a number is not money just because it has a dollar sign.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import salary

RATE = 1.37
TARGET = 80000


class Ranges(unittest.TestCase):
    def test_it_reads_an_annual_range(self):
        pay = salary.parse("The base salary range for this role is "
                           "$95,000 - $120,000 CAD.", "CA")
        self.assertEqual((pay["min"], pay["max"]), (95000, 120000))
        self.assertEqual(pay["currency"], "CAD")
        self.assertTrue(pay["currency_stated"])
        self.assertEqual(pay["period"], "annual")

    def test_it_reads_the_dash_variants(self):
        for text in ("Salary: $90,000 - $110,000",
                     "Salary: $90,000 – $110,000",
                     "Salary: $90,000 to $110,000",
                     "Salary range 90,000 through 110,000"):
            with self.subTest(text=text):
                pay = salary.parse(text, "CA")
                self.assertEqual((pay["min"], pay["max"]), (90000, 110000), text)

    def test_it_reads_a_k_suffix(self):
        pay = salary.parse("Compensation: $90k - $110k annually", "CA")
        self.assertEqual((pay["min"], pay["max"]), (90000, 110000))

    def test_a_single_figure_is_a_point_not_a_floor_of_zero(self):
        pay = salary.parse("Starting salary $88,000 per year.", "CA")
        self.assertEqual((pay["min"], pay["max"]), (88000, 88000))

    def test_a_backwards_range_is_put_the_right_way_round(self):
        pay = salary.parse("Salary: $120,000 to $95,000", "CA")
        self.assertEqual((pay["min"], pay["max"]), (95000, 120000))


class Hourly(unittest.TestCase):
    """$80,000 a year is $38.46 an hour at 40 hours for 52 weeks, which is the
    conversion the target was stated against."""

    def test_it_annualizes_an_hourly_range(self):
        pay = salary.parse("Compensation: $38.50 - $45.00 per hour", "CA")
        self.assertEqual(pay["period"], "hourly")
        self.assertEqual(pay["min"], int(round(38.50 * 2080)))
        self.assertTrue(salary.meets(pay, TARGET, RATE))

    def test_the_boundary_lands_where_the_target_says(self):
        under = salary.parse("Pay: $38.00/hr", "CA")
        over = salary.parse("Pay: $39.00/hr", "CA")
        self.assertFalse(salary.meets(under, TARGET, RATE))
        self.assertTrue(salary.meets(over, TARGET, RATE))

    def test_it_infers_the_period_from_the_magnitude(self):
        # Postings drop the unit constantly. Nobody is paid $95,000 an hour
        # and nobody is paid $42 a year, so the number itself says which.
        self.assertEqual(salary.parse("Salary: $42.00", "CA")["period"], "hourly")
        self.assertEqual(salary.parse("Salary: $95,000", "CA")["period"], "annual")

    def test_an_impossible_hourly_figure_is_thrown_out(self):
        # "$95,000 per hour" is a typo, not a data point.
        self.assertIsNone(salary.parse("Salary: $95,000 per hour", "CA"))


class NotMoney(unittest.TestCase):
    """A number is not pay just because it has a dollar sign in front."""

    def test_funding_is_not_a_salary(self):
        self.assertIsNone(salary.parse(
            "We have raised $95,000,000 in funding and have 200 employees.", "CA"))

    def test_years_of_experience_are_not_a_salary(self):
        self.assertIsNone(salary.parse(
            "You will have 5 years of experience and manage 10,000 servers.", "CA"))

    def test_revenue_and_headcount_are_not_a_salary(self):
        self.assertIsNone(salary.parse(
            "Our 4,000 person company serves 250,000 customers.", "CA"))

    def test_saying_nothing_returns_nothing(self):
        for text in ("A great opportunity on our platform team.", "", None):
            with self.subTest(text=text):
                self.assertIsNone(salary.parse(text, "CA"))


class Currency(unittest.TestCase):
    """The difference between $95,000 USD and $95,000 CAD decides whether a
    posting clears a Canadian target, so it is never guessed silently."""

    def test_a_stated_currency_is_marked_as_stated(self):
        for text, want in (("Salary $95,000 CAD", "CAD"),
                           ("Salary $95,000 USD", "USD")):
            with self.subTest(text=text):
                pay = salary.parse(text, None)
                self.assertEqual(pay["currency"], want)
                self.assertTrue(pay["currency_stated"])

    def test_an_unstated_currency_falls_back_to_the_region_and_says_so(self):
        pay = salary.parse("Salary $95,000", "CA")
        self.assertEqual(pay["currency"], "CAD")
        self.assertFalse(pay["currency_stated"])

    def test_a_us_figure_is_converted_before_it_is_compared(self):
        # $60,000 USD is over the $80,000 CAD target; $50,000 USD is not.
        over = salary.parse("Salary $60,000 USD", None)
        under = salary.parse("Salary $50,000 USD", None)
        self.assertTrue(salary.meets(over, TARGET, RATE))
        self.assertFalse(salary.meets(under, TARGET, RATE))

    def test_an_unknown_currency_is_not_compared_at_all(self):
        pay = salary.parse("Salary $95,000", None)
        self.assertIsNone(pay["currency"])
        self.assertIsNone(salary.meets(pay, TARGET, RATE))


class SilenceIsNotFailure(unittest.TestCase):
    """The rule the whole feature hangs on. Most postings say nothing about
    pay, and reading that as "pays badly" would throw away exactly the roles
    worth opening."""

    def test_no_figure_gives_no_verdict(self):
        self.assertIsNone(salary.meets(None, TARGET, RATE))
        self.assertIsNone(salary.meets(salary.parse("", "CA"), TARGET, RATE))

    def test_a_verdict_is_only_ever_true_or_false_when_there_is_a_figure(self):
        self.assertIs(salary.meets(salary.parse("Salary $90,000 CAD", "CA"),
                                   TARGET, RATE), True)
        self.assertIs(salary.meets(salary.parse("Salary $70,000 CAD", "CA"),
                                   TARGET, RATE), False)


if __name__ == "__main__":
    unittest.main()
