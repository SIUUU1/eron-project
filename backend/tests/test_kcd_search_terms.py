import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.kcd import (
    expand_common_kcd_terms,
    search_kcd_codes,
    search_token_variants,
)
from app.models.base import Base
from app.models.kcd_code import KcdCode


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


class KcdSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(cls.engine)
        with Session(cls.engine) as db:
            db.add_all(
                [
                    KcdCode(
                        code="S22320",
                        name_ko="제1늑골의 골절 골절,폐쇄성",
                        name_en="Fracture of first rib, closed",
                    ),
                    KcdCode(
                        code="S22390",
                        name_ko="상세불명 늑골의 단일 골절,폐쇄성",
                        name_en="Fracture of unspecified one rib, closed",
                    ),
                    KcdCode(
                        code="C413",
                        name_ko="늑골, 흉골 및 쇄골의 악성 신생물",
                        name_en="Malignant neoplasm of ribs, sternum and clavicle",
                    ),
                    KcdCode(
                        code="S8290",
                        name_ko="아래다리의 상세불명 부분의 골절, 폐쇄성",
                        name_en="Fracture of lower leg, part unspecified, closed",
                    ),
                    KcdCode(
                        code="S8291",
                        name_ko="아래다리의 상세불명 부분의 골절, 개방성",
                        name_en="Fracture of lower leg,part unspecified, open",
                    ),
                    KcdCode(
                        code="M0007",
                        name_ko="포도알균성 관절염 및 다발관절염, 발목 및 발",
                        name_en=(
                            "Staphylococcal arthritis and polyarthritis, ankle and foot"
                        ),
                    ),
                    KcdCode(
                        code="S099",
                        name_ko="머리의 상세불명 손상",
                        name_en="Unspecified injury of head",
                    ),
                    KcdCode(
                        code="M6220",
                        name_ko="비외상성 구획증후군, 여러 부위",
                        name_en="Compartment syndrome, non-traumatic, multiple sites",
                    ),
                    KcdCode(
                        code="R074",
                        name_ko="상세불명의 흉통",
                        name_en="Chest pain, unspecified",
                    ),
                    KcdCode(code="R81", name_ko="당뇨", name_en="Glycosuria"),
                    KcdCode(
                        code="E109",
                        name_ko="합병증을 동반하지 않은 1형 당뇨병",
                        name_en="Type 1 diabetes mellitus, without complications",
                    ),
                    KcdCode(
                        code="E119",
                        name_ko="합병증을 동반하지 않은 2형 당뇨병",
                        name_en="Type 2 diabetes mellitus, without complications",
                    ),
                    KcdCode(
                        code="E149",
                        name_ko="합병증을 동반하지 않은 당뇨병 NOS",
                        name_en="Diabetes NOS, without complications",
                    ),
                    KcdCode(
                        code="E1163",
                        name_ko="저혈당을 동반한 2형 당뇨병",
                        name_en="Type 2 diabetes mellitus, with hypoglycemia",
                    ),
                    KcdCode(
                        code="I713",
                        name_ko="파열된 복부대동맥동맥류",
                        name_en="Abdominal aortic aneurysm, ruptured",
                    ),
                    KcdCode(
                        code="C73",
                        name_ko="갑상선의 악성 신생물",
                        name_en="Malignant neoplasm of thyroid gland",
                    ),
                    KcdCode(
                        code="M8007",
                        name_ko="병적 골절을 동반한 폐경후골다공증, 발목 및 발",
                        name_en=(
                            "Postmenopausal osteoporosis with pathological fracture, "
                            "ankle and foot"
                        ),
                    ),
                ]
            )
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def search(self, query: str, limit: int = 10):
        with Session(self.engine) as db:
            return search_kcd_codes(q=query, limit=limit, db=db)

    def test_multiword_english_search_ranks_rows_matching_all_tokens_first(self):
        response = self.search("rib fracture")

        self.assertGreaterEqual(len(response.items), 2)
        self.assertTrue(
            all(
                "rib" in (item.name_en or "").lower()
                and "fracture" in (item.name_en or "").lower()
                for item in response.items[:2]
            )
        )
        first_partial_match = next(
            index
            for index, item in enumerate(response.items)
            if not (
                "rib" in (item.name_en or "").lower()
                and "fracture" in (item.name_en or "").lower()
            )
        )
        self.assertGreaterEqual(first_partial_match, 2)

    def test_single_english_rib_search(self):
        response = self.search("rib")
        self.assertTrue(any("rib" in (item.name_en or "").lower() for item in response.items))

    def test_single_english_fracture_search(self):
        response = self.search("fracture")
        self.assertTrue(
            all("fracture" in (item.name_en or "").lower() for item in response.items)
        )

    def test_other_multiword_english_search_ranks_all_tokens_first(self):
        response = self.search("ankle fracture")
        first = response.items[0]
        combined_name = f"{first.name} {first.name_en or ''}".lower()
        self.assertIn("ankle", combined_name)
        self.assertIn("fracture", combined_name)

    def test_reversed_english_word_order(self):
        response = self.search("head injury")
        self.assertEqual(response.items[0].code, "S09.9")

    def test_plural_query_matches_singular_official_name(self):
        response = self.search("ribs fracture")
        self.assertTrue(
            all(
                "rib" in (item.name_en or "").lower()
                and "fracture" in (item.name_en or "").lower()
                for item in response.items[:2]
            )
        )

    def test_hyphen_and_space_are_equivalent_for_token_search(self):
        response = self.search("non traumatic")
        self.assertEqual(response.items[0].code, "M62.20")

    def test_extra_spaces_and_case_do_not_prevent_multiword_search(self):
        response = self.search("  RIB   FRACTURE  ")
        self.assertTrue(
            "rib" in (response.items[0].name_en or "").lower()
            and "fracture" in (response.items[0].name_en or "").lower()
        )

    def test_tokenizer_splits_punctuation_and_builds_plural_variant(self):
        self.assertEqual(
            search_token_variants("ribs/non-traumatic"),
            (("ribs", "rib"), ("non",), ("traumatic",)),
        )

    def test_korean_diagnosis_search(self):
        response = self.search("제1늑골의 골절")
        self.assertEqual(response.items[0].code, "S22.320")

    def test_direct_kcd_code_search(self):
        response = self.search("S22.320")
        self.assertEqual(response.items[0].code, "S22.320")

    def test_kcd_code_search_without_dot(self):
        response = self.search("s22320")
        self.assertEqual(response.items[0].code, "S22.320")

    def test_exact_english_name_keeps_priority_over_token_matches(self):
        response = self.search("Chest pain, unspecified")
        self.assertEqual(response.items[0].code, "R07.4")

    def test_korean_diabetes_keeps_exact_name_first_and_surfaces_e119(self):
        response = self.search("당뇨")

        self.assertEqual(response.items[0].code, "R81")
        self.assertIn("E11.9", [item.code for item in response.items[:3]])

    def test_korean_diabetes_name_surfaces_uncomplicated_codes_first(self):
        response = self.search("당뇨병")
        top_codes = [item.code for item in response.items[:3]]

        self.assertIn("E11.9", top_codes)
        self.assertNotIn("E11.63", top_codes)

    def test_english_diabetes_surfaces_uncomplicated_codes_first(self):
        response = self.search("diabetes")
        top_codes = [item.code for item in response.items[:3]]

        self.assertIn("E11.9", top_codes)
        self.assertNotIn("E11.63", top_codes)

    def test_existing_alias_search_is_preserved(self):
        with patch(
            "app.api.kcd.lookup_alias_terms",
            return_value=("복부대동맥류", "abdominal aortic aneurysm"),
        ):
            response = self.search("AAA")

        self.assertEqual(response.items[0].code, "I71.3")

    def test_existing_cancer_expansion_search_is_preserved(self):
        response = self.search("갑상선암")
        self.assertEqual(response.items[0].code, "C73")

    def test_unknown_diagnosis_returns_no_results(self):
        response = self.search("zzzznosuchkcdterm")
        self.assertEqual(response.items, [])
        self.assertEqual(response.total, 0)


if __name__ == "__main__":
    unittest.main()
