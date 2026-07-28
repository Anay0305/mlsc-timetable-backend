from __future__ import annotations

import unittest

from server.routers.change_requests import _subject_name_match


class SubjectRequestVerificationTests(unittest.TestCase):
    def test_match_is_case_and_punctuation_insensitive(self) -> None:
        self.assertEqual(
            _subject_name_match("Artificial Intelligence", "ARTIFICIAL intelligence"),
            1.0,
        )
        punctuation_score = _subject_name_match(
            "Data Structures & Algorithms",
            "data structures and algorithms",
        )
        self.assertGreater(punctuation_score, 0.8)
        self.assertLess(punctuation_score, 0.95)

    def test_small_typo_can_clear_ninety_five_percent_threshold(self) -> None:
        score = _subject_name_match(
            "Advanced Computer Architecture",
            "Advanced Computer Architectures",
        )
        self.assertGreaterEqual(score, 0.95)

    def test_different_name_needs_review(self) -> None:
        score = _subject_name_match("Computer Networks", "Operating Systems")
        self.assertLess(score, 0.95)

    def test_empty_name_never_matches(self) -> None:
        self.assertEqual(_subject_name_match("", "Operating Systems"), 0.0)


if __name__ == "__main__":
    unittest.main()
