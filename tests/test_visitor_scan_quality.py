"""Conservative automation boundaries for the real scan path."""

import json
import unittest
from time import monotonic
from unittest.mock import patch

from visa_agent.document_pipeline.ocr import default_ocr_provider
from visa_agent.document_pipeline.quality import _mrz_checksum, passport_fields_conflict, read_passport, scan_review

PASSPORT_TEXT = """PASSPORT
Passport number: ZT1234567
Surname: TESTER
Given names: AVERY
Date of birth: 1991-02-13
Date of expiry: 2031-02-13"""


class VisitorScanQualityTests(unittest.TestCase):
    def assess(self, *, filename="scan.pdf", text=PASSPORT_TEXT, sharpness=0.9, edge=False):
        return scan_review(
            filename=filename,
            ocr_text=text,
            ocr_status="completed",
            ocr_detail=json.dumps({"pages": [{"sharpness": sharpness, "touches_edge": edge}]}),
            visitor_passport_auto=True,
        )

    def test_anonymous_passport_scan_reaches_blurry_and_clear_states(self):
        label, blurry, _ = self.assess(sharpness=0.05)
        self.assertEqual(label, "passport")
        self.assertEqual((blurry.code, blurry.severity), ("blurry", "simple_auto"))
        label, clear, passport = self.assess()
        self.assertEqual((label, clear.code), ("passport", "ok"))
        self.assertEqual(passport.fields["number"], "ZT1234567")

    def test_filename_and_ocr_disagreement_goes_to_an_advisor(self):
        for filename in ("bank-statement.pdf", "passport-and-bank.pdf"):
            with self.subTest(filename=filename):
                label, finding, _ = self.assess(filename=filename)
                self.assertEqual((label, finding.severity), ("unclassified", "complex_human"))

    def test_agent_classification_is_kept_independently_of_the_quality_result(self):
        text = """护照
护照号码/Passport No.
P
UTO
ZT1234567
姓名/Name
测试名
TESTER, AVERY
出生日期/Date of birth
13 FEB 1991
有效期至/Date of expiry
13 2月/FEB 2031"""
        for ocr, expected in [(text, "ok"), ("", "incomplete_scan")]:
            with self.subTest(expected=expected):
                label, finding, _ = scan_review(
                    filename="bank-statement.jpg", ocr_text=ocr, ocr_status="completed",
                    ocr_detail=json.dumps({"pages": [{"sharpness": 0.35, "touches_edge": False}]}),
                    visitor_passport_auto=True, agent_classification="passport",
                )
                self.assertEqual((label, finding.code), ("passport", expected))

    def test_name_or_unrelated_content_alone_does_not_classify_a_passport(self):
        label, finding, _ = self.assess(filename="passport.pdf", text="Nationality field")
        self.assertEqual((label, finding.code), ("unclassified", "unclear_classification"))

    def test_typed_td3_document_with_valid_checks_is_a_clear_passport(self):
        number, birth, expiry = "XX0000001", "910213", "310213"
        names = "POUTO" + "TESTER<<AVERY".ljust(39, "<")
        credentials = (
            number + _mrz_checksum(number) + "UTO" + birth + _mrz_checksum(birth)
            + "X" + expiry + _mrz_checksum(expiry) + "<" * 14 + "00"
        )
        label, finding, reading = self.assess(text=f"{names}\n{credentials}")
        self.assertEqual((label, finding.code, reading.complete), ("passport", "ok", True))
        _, uncertain, _ = self.assess(text=f"{names}\n{credentials[:9]}{(int(credentials[9])+1)%10}{credentials[10:]}")
        self.assertEqual(uncertain.code, "unclear_classification")

    def test_chinese_identity_card_is_kept_as_extra_material(self):
        label, finding, _ = self.assess(text="测试样本\n中华人民共和国居民身份证\n公民身份号码")
        self.assertEqual((label, finding.code, finding.severity),
                         ("unclassified", "optional_identity_card", "simple_auto"))

    def test_ambiguous_sharpness_missing_fields_and_cropping_go_to_an_advisor(self):
        scenarios = ({"sharpness": 0.20}, {"edge": True},
                     {"text": PASSPORT_TEXT.split("Date of expiry:")[0]})
        for keyword_arguments in scenarios:
            with self.subTest(**keyword_arguments):
                _, finding, _ = self.assess(**keyword_arguments)
                self.assertEqual(finding.severity, "complex_human")

    def test_conflicting_numbers_and_names_are_detected(self):
        original = read_passport(PASSPORT_TEXT)
        for revised in (PASSPORT_TEXT.replace("ZT1234567", "ZT1234569"),
                        PASSPORT_TEXT.replace("AVERY", "AUBREY")):
            self.assertTrue(passport_fields_conflict(original, read_passport(revised)))
        _, finding, _ = self.assess(text=PASSPORT_TEXT + "\nPassport number: ZT1234569")
        self.assertEqual(finding.code, "passport_data_conflict")

    def test_real_ocr_error_never_falls_back_to_synthetic_markers(self):
        with patch.dict("os.environ", {"VISA_AGENT_VISION_OCR": "/missing/visa-ocr-helper"}):
            result = default_ocr_provider().extract(
                b"%PDF-1.4 PASSPORT BLURRY", filename="passport.pdf", content_type="application/pdf",
            )
        self.assertEqual((result.status, result.text), ("failed", ""))
        label, finding, _ = scan_review(
            filename="passport.pdf", ocr_text=result.text, ocr_status=result.status,
            ocr_detail=result.detail, visitor_passport_auto=True,
        )
        self.assertEqual((label, finding.severity), ("unclassified", "complex_human"))


class TranslationSourceChecks(unittest.TestCase):
    def test_invalid_json_falls_back_to_plain_translation_with_original_figures(self):
        from unittest.mock import Mock

        from visa_agent.ai_capabilities.document_translation import _translate_lines

        client = Mock()
        client.post.return_value.json.side_effect = [
            {"message": {"content": '{"lines": ['}},
            {"message": {"content": "Account balance: [[N1]] yuan"}},
        ]
        source = "账户余额：1234.50 元"
        self.assertEqual(
            _translate_lines(client, [source, "938104"], monotonic() + 5),
            ["Account balance: 1234.50 yuan", "938104"],
        )
        self.assertNotIn("format", client.post.call_args.kwargs["json"])

    def test_docx_plain_text_preserves_paragraphs_and_numbers(self):
        from io import BytesIO
        from zipfile import ZipFile

        from visa_agent.document_pipeline.ocr import LocalScanOcrProvider

        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("word/media/", b"")
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>在职证明</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>月薪 </w:t></w:r><w:r><w:t>1234.50 元</w:t></w:r></w:p></w:body></w:document>'
            ))
        result = LocalScanOcrProvider().extract(data.getvalue(), filename="Work.docx", content_type="")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "在职证明\n月薪 1234.50 元")

    def test_refuse_model_output_that_changes_the_currency_or_amount(self):
        from unittest.mock import Mock

        from visa_agent.ai_capabilities.document_translation import _translate_batch

        client = Mock()
        for invalid in ("Account balance: 1234.50 USD", "Account balance: 1234.05 CNY"):
            with self.subTest(translation=invalid):
                client.post.return_value.json.return_value = {
                    "message": {"content": json.dumps({"lines": [invalid]})}
                }
                with self.assertRaises(ValueError):
                    _translate_batch(client, ["账户余额：1234.50 元"], monotonic() + 5)
