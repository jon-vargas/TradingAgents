import datetime
import unittest

import typer

from cli.utils import (
    normalize_date_option,
    normalize_ticker,
    normalize_ticker_list,
    validate_date_range,
)


class TestCliValidation(unittest.TestCase):
    def test_normalize_ticker(self):
        self.assertEqual(normalize_ticker("aapl"), "AAPL")
        self.assertEqual(normalize_ticker("brk.b"), "BRK.B")
        with self.assertRaises(typer.BadParameter):
            normalize_ticker("")
        with self.assertRaises(typer.BadParameter):
            normalize_ticker("TOO-LONGGG")

    def test_normalize_ticker_list(self):
        self.assertEqual(normalize_ticker_list("AAPL, msft"), ["AAPL", "MSFT"])
        with self.assertRaises(typer.BadParameter):
            normalize_ticker_list("")

    def test_normalize_date_option(self):
        self.assertEqual(
            normalize_date_option("2020-01-01", label="analysis", allow_future=False),
            "2020-01-01",
        )
        with self.assertRaises(typer.BadParameter):
            normalize_date_option("2020/01/01", label="analysis", allow_future=False)

    def test_normalize_date_option_future(self):
        future = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        with self.assertRaises(typer.BadParameter):
            normalize_date_option(future, label="analysis", allow_future=False)

    def test_validate_date_range(self):
        validate_date_range("2020-01-01", "2020-01-02")
        with self.assertRaises(typer.BadParameter):
            validate_date_range("2020-01-02", "2020-01-01")
