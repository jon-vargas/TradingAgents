import unittest

from tradingagents.agents.rating import extract_rating_from_label, normalize_rating_for_display


class TestRatingLabelParsing(unittest.TestCase):
    def test_extracts_canonical_line(self):
        text = (
            'DECISION_JSON: {"decision":"BUY"}\n'
            "Rating: BUY\n"
            "Decision rationale follows."
        )
        self.assertEqual(extract_rating_from_label(text), "BUY")

    def test_ignores_blockquote(self):
        text = "> Bull said Rating: BUY\nRating: HOLD\n"
        self.assertEqual(extract_rating_from_label(text), "HOLD")

    def test_review_token(self):
        self.assertEqual(extract_rating_from_label("Rating: REVIEW"), "REVIEW")

    def test_normalize_display(self):
        self.assertEqual(normalize_rating_for_display("buy"), "BUY")


if __name__ == "__main__":
    unittest.main()
