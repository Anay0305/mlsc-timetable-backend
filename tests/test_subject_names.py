"""Subject-name normalization for admin-entered catalog rows."""

from __future__ import annotations

import unittest

from server.storage import _camel_subject_name as normalize


class SubjectNameTests(unittest.TestCase):
    def test_plain_titles_are_title_cased(self):
        self.assertEqual(normalize("lean manufacturing"), "Lean Manufacturing")
        self.assertEqual(normalize("METAL FORMING"), "Metal Forming")

    def test_acronyms_survive_title_casing(self):
        # A real admin add stored "Biosensors And Mems" because MEMS was not
        # on the allowlist; an initialism must never become a word.
        self.assertEqual(normalize("Biosensors and MEMS"), "Biosensors And MEMS")
        self.assertEqual(normalize("CAD for VLSI"), "CAD For VLSI")
        self.assertEqual(normalize("Data Science: Computer Vision and NLP"),
                         "Data Science: Computer Vision And NLP")

    def test_lowercase_acronyms_are_raised_to_canonical_form(self):
        self.assertEqual(normalize("plc and scada"), "PLC And SCADA")
        self.assertEqual(normalize("embedded system design and iot"),
                         "Embedded System Design And IoT")

    def test_punctuation_around_an_acronym_is_kept(self):
        self.assertEqual(normalize("(vlsi) design"), "(VLSI) Design")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize("  metal    forming "), "Metal Forming")

    def test_empty_input_yields_empty(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")


if __name__ == "__main__":
    unittest.main()
