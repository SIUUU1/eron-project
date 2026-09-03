import unittest

from app.api.kcd import expand_common_kcd_terms


class KcdSearchTermExpansionTests(unittest.TestCase):
    def test_expands_korean_cancer_name_to_kcd_wording(self):
        self.assertEqual(
            expand_common_kcd_terms("갑상선암"),
            ("갑상선의 악성 신생물",),
        )

    def test_expands_english_cancer_name_to_kcd_wording(self):
        self.assertEqual(
            expand_common_kcd_terms("Thyroid cancer"),
            ("malignant neoplasm of Thyroid",),
        )

    def test_does_not_expand_unrelated_search_terms(self):
        self.assertEqual(expand_common_kcd_terms("갑상선 기능 저하증"), ())

    def test_expands_colon_cancer_to_colon_and_rectum_wording(self):
        # KCD has no "대장의 악성 신생물" entry; colon/rectum are coded separately.
        self.assertEqual(
            expand_common_kcd_terms("대장암"),
            ("결장의 악성 신생물", "직장의 악성 신생물"),
        )

    def test_expands_liver_cancer_to_include_bile_duct_wording(self):
        # The primary KCD code (C22) is "간 및 간내 담관의 악성 신생물", not "간의 악성 신생물".
        self.assertEqual(
            expand_common_kcd_terms("간암"),
            ("간의 악성 신생물", "간 및 간내 담관의 악성 신생물"),
        )

    def test_expands_uterine_cancer_to_cervix_and_corpus_wording(self):
        # KCD has no bare "자궁의 악성 신생물" entry; split into cervix/corpus.
        self.assertEqual(
            expand_common_kcd_terms("자궁암"),
            ("자궁경부의 악성 신생물", "자궁체부의 악성 신생물"),
        )

    def test_expands_nose_cancer_to_nasal_cavity_wording(self):
        self.assertEqual(
            expand_common_kcd_terms("코암"),
            ("비강의 악성 신생물",),
        )


if __name__ == "__main__":
    unittest.main()
