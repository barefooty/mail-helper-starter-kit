#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mask_sensitive() 的單元測試（純標準庫 unittest）。

執行方式（在 進階版_雲端程式/ 目錄）：
    PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gmail_triage import mask_sensitive, MASK_CHAR  # noqa: E402

O = MASK_CHAR


class ShouldMask(unittest.TestCase):
    """每種已知樣式都要被換成等長的「○」。"""

    def test_taiwan_id(self):
        self.assertEqual(mask_sensitive("身分證 A123456789 請查收"), "身分證 " + O * 10 + " 請查收")
        self.assertEqual(mask_sensitive("F212345678"), O * 10)

    def test_mobile(self):
        self.assertEqual(mask_sensitive("手機 0912345678"), "手機 " + O * 10)
        self.assertEqual(mask_sensitive("0912-345-678"), O * 12)
        self.assertEqual(mask_sensitive("0912 345 678"), O * 12)

    def test_landline(self):
        self.assertEqual(mask_sensitive("電話 02-2345-6789"), "電話 " + O * 12)
        self.assertEqual(mask_sensitive("(02)2345-6789"), O * 13)
        self.assertEqual(mask_sensitive("03-123-4567"), O * 11)
        self.assertEqual(mask_sensitive("037-123456"), O * 10)

    def test_landline_with_extension(self):
        self.assertEqual(mask_sensitive("02-2345-6789 分機 123"), O * 19)
        self.assertEqual(mask_sensitive("02-2345-6789#12345"), O * 18)

    def test_credit_card_and_long_digits(self):
        self.assertEqual(mask_sensitive("卡號 1234567890123456"), "卡號 " + O * 16)
        self.assertEqual(mask_sensitive("1234 5678 9012 3456"), O * 19)
        self.assertEqual(mask_sensitive("帳戶 0123456789 匯入"), "帳戶 " + O * 10 + " 匯入")  # 剛好 10 碼

    def test_bank_account_hyphen_groups(self):
        self.assertEqual(mask_sensitive("帳號 012-34-567890-1"), "帳號 " + O * 15)

    def test_password_label_keeps_label_masks_value(self):
        self.assertEqual(mask_sensitive("密碼：Abc12345"), "密碼：" + O * 8)
        self.assertEqual(mask_sensitive("password: p@ss-word!"), "password: " + O * 10)
        self.assertEqual(mask_sensitive("通行碼=Xyz98765"), "通行碼=" + O * 8)

    def test_account_label_non_email_value(self):
        self.assertEqual(mask_sensitive("帳號：s10912345"), "帳號：" + O * 9)

    def test_student_id_with_label(self):
        self.assertEqual(mask_sensitive("學號 109123456 王小明"), "學號 " + O * 9 + " 王小明")
        self.assertEqual(mask_sensitive("學號：B10912345"), "學號：" + O * 9)
        self.assertEqual(mask_sensitive("Student ID: 1091234567"), "Student ID: " + O * 10)

    def test_password_that_looks_like_email_still_masked(self):
        self.assertEqual(mask_sensitive("密碼：me@mail.com"), "密碼：" + O * 11)

    def test_multiple_in_one_text(self):
        src = "學生 A123456789，手機 0912345678，密碼：abc123，請協助。"
        out = mask_sensitive(src)
        self.assertNotIn("A123456789", out)
        self.assertNotIn("0912345678", out)
        self.assertNotIn("abc123", out)
        self.assertIn("學生 ", out)
        self.assertIn("請協助", out)
        self.assertIn("密碼：", out)


class ShouldNotMask(unittest.TestCase):
    """日常業務文字不能被誤遮。"""

    def assertUnchanged(self, text):
        self.assertEqual(mask_sensitive(text), text)

    def test_dates(self):
        self.assertUnchanged("2026-10-16")
        self.assertUnchanged("2026/10/16 08:30")
        self.assertUnchanged("10/16 截止")
        self.assertUnchanged("115-1 學期")
        self.assertUnchanged("2026-10-16 15:00 場")   # 日期後接空格與數字，不可串成長數字

    def test_amounts_under_ten_digits(self):
        self.assertUnchanged("服務費 342,800 元")
        self.assertUnchanged("總價 1200000")        # 7 碼
        self.assertUnchanged("共 38 門課、124 人")

    def test_course_codes(self):
        self.assertUnchanged("課程代碼 GEM114A001")
        self.assertUnchanged("課號 1141CS0012")      # 含英文字母，不是連續數字

    def test_bare_nine_digits_without_label(self):
        self.assertUnchanged("編號 109123456")        # 沒有「學號」標籤的 9 碼不遮（不足 10 碼）

    def test_email_kept_even_after_account_label(self):
        self.assertUnchanged("帳號：staff@example.edu.tw")
        self.assertUnchanged("寄件人 ge@example-a.edu.tw")

    def test_lowercase_or_wrong_second_char_not_id(self):
        self.assertUnchanged("a123456789")           # 小寫不是身分證格式
        self.assertUnchanged("A323456789")           # 第二碼非 1/2

    def test_id_inside_longer_token(self):
        self.assertUnchanged("REF-A123456789X")      # 前後接英數，不視為獨立身分證

    def test_chinese_label_value_not_masked(self):
        self.assertUnchanged("帳號：尚未建立")         # 值不是帳密樣式

    def test_empty_and_none(self):
        self.assertEqual(mask_sensitive(""), "")
        self.assertIsNone(mask_sensitive(None))


class ConfigSwitch(unittest.TestCase):
    def test_default_on(self):
        import gmail_triage
        self.assertTrue(gmail_triage._DEFAULTS["mask_sensitive"])


if __name__ == "__main__":
    unittest.main()
