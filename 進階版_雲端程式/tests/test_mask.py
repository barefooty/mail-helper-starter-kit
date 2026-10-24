#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mask_sensitive()／出口打碼的單元測試（純標準庫 unittest）。

2026-10-20 定版「留頭尾、遮中間」：號碼只遮中間約四成（0912○○○○78、A212○○○○57、311○○○034），
密碼、通行碼全遮（○○○○○○○○）；學校、單位名不遮；人名改 O（王O明）。

執行方式（在 repo 根目錄）：
    PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -v

三處（信箱小幫手起始包進階版、SOS、跨校通識）共用同一份；改規則請三處一起改、一起跑。
- ShouldMask／ShouldNotMask：程式層規則（讀信時先把已知樣式留頭尾、遮中間）。
- RealWorldSamples：兩個計畫信箱近期實際來信的樣式（姓名、學校、號碼已去識別、只保留形狀），
  記錄「這類信進來會變什麼樣」，含幾個已知的過度遮罩（網址參數、公文字號）。
- ExitMask：出口打碼（2026-10-20）——寄出前對報告全文把寄件人人名改「王O明」、號碼再遮一次，HTML 只動文字節點。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gmail_triage import (mask_sensitive, MASK_CHAR, name_from_display, name_mask,  # noqa: E402
                          mask_report_text, mask_report_html, _display_and_domain)

O = MASK_CHAR


class ShouldMask(unittest.TestCase):
    """每種已知樣式都要被「留頭尾、遮中間」（密碼全遮）。"""

    def test_taiwan_id(self):
        self.assertEqual(mask_sensitive("身分證 A123456789 請查收"), "身分證 A123" + O * 4 + "89 請查收")
        self.assertEqual(mask_sensitive("F212345678"), "F212" + O * 4 + "78")

    def test_mobile(self):
        self.assertEqual(mask_sensitive("手機 0912345678"), "手機 0912" + O * 4 + "78")
        self.assertEqual(mask_sensitive("0912-345-678"), "0912-○○○-○78")   # 分隔符原樣，只遮數字
        self.assertEqual(mask_sensitive("0912 345 678"), "0912 ○○○ ○78")

    def test_landline(self):
        self.assertEqual(mask_sensitive("電話 02-2345-6789"), "電話 02-23○○-○○89")
        self.assertEqual(mask_sensitive("(02)2345-6789"), "(02)23○○-○○89")
        self.assertEqual(mask_sensitive("03-123-4567"), "03-1○○-○567")
        self.assertEqual(mask_sensitive("037-123456"), "037-○○○456")

    def test_landline_with_extension(self):
        self.assertEqual(mask_sensitive("02-2345-6789 分機 123"), "02-234○-○○○○ 分機 123")   # 「分機」與分機號留著
        self.assertEqual(mask_sensitive("02-2345-6789#12345"), "02-23○○-○○○○#○○○45")

    def test_credit_card_and_long_digits(self):
        self.assertEqual(mask_sensitive("卡號 1234567890123456"), "卡號 1234" + O * 10 + "56")   # 14 碼以上純數字只留頭 4 尾 2
        self.assertEqual(mask_sensitive("1234 5678 9012 3456"), "1234 ○○○○ ○○○○ ○○56")
        self.assertEqual(mask_sensitive("帳戶 0123456789 匯入"), "帳戶 0123○○○○89 匯入")  # 剛好 10 碼

    def test_bank_account_hyphen_groups(self):
        self.assertEqual(mask_sensitive("帳號 012-34-567890-1"), "帳號 012-34-5○○○○0-1")

    def test_password_label_keeps_label_masks_whole_value(self):
        # 密碼、通行碼：全遮（2026-10-24 定：摘要上不得出現任何密碼內容）；密碼內容不寫進摘要是報告端規則
        self.assertEqual(mask_sensitive("密碼：Abc12345"), "密碼：" + O * 8)
        self.assertEqual(mask_sensitive("password: p@ss-word!"), "password: " + O * 10)
        self.assertEqual(mask_sensitive("通行碼=Xyz98765"), "通行碼=" + O * 8)

    def test_account_label_non_email_value(self):
        self.assertEqual(mask_sensitive("帳號：s10912345"), "帳號：s10○○○345")   # 帳號留頭尾

    def test_student_id_with_label(self):
        self.assertEqual(mask_sensitive("學號 109123456 王小明"), "學號 109○○○456 王小明")
        self.assertEqual(mask_sensitive("學號：B10912345"), "學號：B10○○○345")
        self.assertEqual(mask_sensitive("Student ID: 1091234567"), "Student ID: 1091○○○○67")

    def test_alnum_student_id_without_label(self):
        # 2026-10-20 新增：字母＋8～9 碼（s11564026 這種學號型代號）沒有「學號」標籤也遮
        self.assertEqual(mask_sensitive("寄件人 s11564026 來信"), "寄件人 s11○○○026 來信")
        self.assertEqual(mask_sensitive("B11500001"), "B11○○○001")
        self.assertEqual(mask_sensitive("a123456789"), "a123○○○○89")   # 小寫＋9 碼：不是身分證格式，但符合學號型，一樣遮
        self.assertEqual(mask_sensitive("A323456789"), "A323○○○○89")   # 第二碼非 1/2：同上

    def test_email_local_digits(self):
        # 2026-10-20 新增：email 本地端 6 碼以上的數字串遮掉、網域保留（學校帳號＝學號@校方網域）
        self.assertEqual(mask_sensitive("帳號 413000456@cloud.example.edu.tw"), "帳號 413○○○456@cloud.example.edu.tw")
        self.assertEqual(mask_sensitive("s411000789@ms.example.edu.tw"), "s411○○○789@ms.example.edu.tw")
        self.assertEqual(mask_sensitive("C119000123@example-u.edu.tw"), "C119○○○123@example-u.edu.tw")

    def test_password_that_looks_like_email_still_masked(self):
        self.assertEqual(mask_sensitive("密碼：me@mail.com"), "密碼：" + O * 11)

    def test_multiple_in_one_text(self):
        src = "學生 A123456789，手機 0912345678，密碼：abc123，請協助。"
        out = mask_sensitive(src)
        self.assertEqual(out, "學生 A123○○○○89，手機 0912○○○○78，密碼：○○○○○○，請協助。")
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
        self.assertUnchanged("demo cou08")

    def test_bare_nine_digits_without_label(self):
        self.assertUnchanged("編號 109123456")        # 沒有「學號」標籤的 9 碼純數字不遮（不足 10 碼）

    def test_email_without_digits_kept(self):
        self.assertUnchanged("帳號：staff@example.edu.tw")
        self.assertUnchanged("寄件人 ge@example-a.edu.tw")
        self.assertUnchanged("abc12345@example.com")   # 本地端只有 5 碼數字

    def test_short_alnum_tokens_kept(self):
        self.assertUnchanged("a1234567")             # 字母＋7 碼：不足 8 碼不當學號
        self.assertUnchanged("abc123456789")         # 三個字母開頭不符學號型

    def test_id_inside_longer_token(self):
        self.assertUnchanged("REF-A123456789X")      # 前後接英數，不視為獨立身分證

    def test_chinese_label_value_not_masked(self):
        self.assertUnchanged("帳號：尚未建立")         # 值不是帳密樣式

    def test_empty_and_none(self):
        self.assertEqual(mask_sensitive(""), "")
        self.assertIsNone(mask_sensitive(None))


class RealWorldSamples(unittest.TestCase):
    """常見來信樣式（虛構改寫）：確認學生常見寫法的實際行為。"""

    def assertUnchanged(self, text):
        self.assertEqual(mask_sensitive(text), text)

    # ---- 計畫信箱 A：帳號相關來信 ----
    def test_a_school_account_local_digits_masked(self):
        # 學校帳號＝9 碼學號@校方網域：本地端數字遮、網域留，承辦人仍看得出是哪所學校的帳號
        self.assertEqual(mask_sensitive("就讀學校：甲大學 \n姓名：王O明 \n使用的平臺帳號：413000456@cloud.example.edu.tw"),
                         "就讀學校：甲大學 \n姓名：王O明 \n使用的平臺帳號：413○○○456@cloud.example.edu.tw")
        self.assertEqual(mask_sensitive("目前使用的平台帳號：s411000789@ms.example.edu.tw"), "目前使用的平台帳號：s411○○○789@ms.example.edu.tw")
        self.assertEqual(mask_sensitive("林O華 <x00010001@example.edu.tw> 於 2026年10月"), "林O華 <x000○○○01@example.edu.tw> 於 2026年10月")

    def test_a_student_id_after_label_masked(self):
        self.assertEqual(mask_sensitive("學號：C119000123"), "學號：C119○○○○23")      # 字母開頭型學號（長得像身分證，兩條規則都會遮）
        self.assertEqual(mask_sensitive("姓名：陳O（學號415000987）"), "姓名：陳O（學號415○○○987）")
        self.assertEqual(mask_sensitive("學號：01500001 \n課程：（課名略）"), "學號：015○○○01 \n課程：（課名略）")
        self.assertEqual(mask_sensitive("你好,我是學號B11500001的學生"), "你好,我是學號B11○○○001的學生")

    def test_a_login_instruction_mail_kept(self):
        # 我們回信裡的「帳號：email／密碼：您的學號（…）」：值是沒有數字串的 email 或中文 → 不遮
        self.assertUnchanged("帳號：B****0001@example.edu.tw \n密碼：您的學號（第一個字母請大寫）")

    def test_a_contact_block_phones_masked(self):
        self.assertEqual(mask_sensitive("聯絡人：王O明 \n聯絡電話：0966-000-123 \nEmail：abc@example.com"),
                         "聯絡人：王O明 \n聯絡電話：0966-○○○-○23 \nEmail：abc@example.com")
        self.assertEqual(mask_sensitive("連絡電話：07-3610000#20001"), "連絡電話：07-36○○○○○#○○○01")
        self.assertEqual(mask_sensitive("電話：03-5550000 分機50001 \n服務時間"), "電話：03-55500○○ 分機○○○01 \n服務時間")
        self.assertEqual(mask_sensitive("收件人：林O華 小姐｜電話：(08) 7790-000 分機 8001"), "收件人：林O華 小姐｜電話：(08) 779○-○○○ 分機 ○001")
        self.assertEqual(mask_sensitive("+886-3-555-2121 <tel>"), "+886-3-○○○-○121 <tel>")

    def test_a_signature_and_urls_kept(self):
        self.assertUnchanged("https://example.notion.site/114-1-246fedcb0a1b80000000a1b2c3d4")  # 連字號分組不足 10 碼
        self.assertUnchanged("服務時間：週一至週五（09:30-17:30）")
        self.assertUnchanged("收據開立需 7-14 個工作天")
        self.assertUnchanged("課程：示範課程（115-1學分班）")

    def test_a_email_local_part_phone_shape(self):
        # email 本地端剛好是手機／10 碼形狀：本地端遮、網域留；寄件人欄另由出口打碼改 O
        self.assertEqual(mask_sensitive("使用的平台帳號為a0958000123@example.com"), "使用的平台帳號為a0958○○○○23@example.com")
        self.assertEqual(mask_sensitive("使用q3025000123@example.com"), "使用q3025○○○○23@example.com")

    # ---- 計畫信箱 B：一般來信 ----
    def test_b_interview_invitation_contact(self):
        src = "研究團隊：王O明、李O \n聯絡人：王O明 \n聯絡電話：0966-000-123 \nEmail：abc@example.edu.tw"
        self.assertEqual(mask_sensitive(src), src.replace("0966-000-123", "0966-○○○-○23"))

    def test_b_spam_tracking_params_overmask_known(self):
        # 垃圾信匣常見：網址參數裡 10 碼以上數字、字母＋8 碼的主機名會被遮，對報告無影響（本來就只一句帶過）
        self.assertEqual(mask_sensitive("https://www.facebook.com/nd/?aref=1791948276862328&medium=email"),
                         "https://www.facebook.com/nd/?aref=1791○○○○○○○○○○28&medium=email")
        self.assertEqual(mask_sensitive("https://u15366979.ct.sendgrid.net/ls/click"), "https://u15○○○979.ct.sendgrid.net/ls/click")

    def test_b_official_document_number_overmask_known(self):
        # 公文字號 10 碼也會被遮（已知誤傷；這類信通常是募款／廣告，報告只一句帶過）
        self.assertEqual(mask_sensitive("設立字號：新北府社助字第1080000001號"), "設立字號：新北府社助字第1080○○○○01號")


class PartialShape(unittest.TestCase):
    """留頭尾、遮中間的形狀（2026-10-20 定版的三個例子）。"""

    def test_examples_from_user(self):
        self.assertEqual(mask_sensitive("0912345678"), "0912○○○○78")
        self.assertEqual(mask_sensitive("A212345657"), "A212○○○○57")
        self.assertEqual(mask_sensitive("學號 311123034"), "學號 311○○○034")

    def test_school_and_unit_names_untouched(self):
        self.assertEqual(mask_sensitive("國立示範科技大學 通識教育中心 王O明 0912345678"),
                         "國立示範科技大學 通識教育中心 王O明 0912○○○○78")


class ExitMask(unittest.TestCase):
    """出口打碼：寄出前對報告全文把人名改 O、號碼再遮；HTML 只動文字節點。"""

    def test_name_mask_shapes(self):
        self.assertEqual(name_mask("王小明"), "王O明")
        self.assertEqual(name_mask("陳明"), "陳O")
        self.assertEqual(name_mask("歐陽小明"), "歐陽OO")
        self.assertEqual(name_mask("A"), "A")

    def test_name_from_display(self):
        self.assertEqual(name_from_display("王小明"), "王小明")
        self.assertEqual(name_from_display("王小明 經由(demo cou08)"), "王小明")
        self.assertEqual(name_from_display("四視光一A王小明"), "王小明")          # 系級前綴＋名字（中間有英數）
        self.assertEqual(name_from_display("人力資源發展系李小華"), "李小華")    # 系名直接接名字：取尾 3 字
        self.assertEqual(name_from_display("博雅教育中心張大同"), "張大同")
        self.assertEqual(name_from_display("黃聆"), "黃聆")                      # 兩字名：顯示名就是那兩字
        self.assertIsNone(name_from_display("通識中心"))                        # 單位名
        self.assertIsNone(name_from_display("SOS 表單通知"))
        self.assertIsNone(name_from_display("demo學網"))                       # 兩字候選但顯示名不只那兩字
        self.assertIsNone(name_from_display("ABCMOOC 經由(demo cou08)"))       # 沒有中文
        self.assertIsNone(name_from_display("Hao-Yi Chen"))
        self.assertIsNone(name_from_display(""))
        self.assertIsNone(name_from_display("純"))                               # 單字不算人名
        self.assertIsNone(name_from_display("排名軟體"))                         # 4 字非複姓＝單位／廣告名
        self.assertEqual(name_from_display("歐陽小明"), "歐陽小明")              # 複姓 4 字才算
        self.assertIsNone(name_from_display("數位學習中心 經由(demo cou08)"))

    def test_display_and_domain(self):
        self.assertEqual(_display_and_domain('"王小明" <s11500001@ms.example.edu.tw>'), ("王小明", "ms.example.edu.tw"))
        self.assertEqual(_display_and_domain("王小明 經由(demo cou08) <noreply@example.org>"), ("王小明 經由(demo cou08)", "example.org"))
        self.assertEqual(_display_and_domain("someone@example.com"), ("", "example.com"))

    def test_mask_report_text_names_and_numbers(self):
        out, hits = mask_report_text("王小明（甲大學）｜學號 109123456 無法簽到；李小華也來信", {"王小明", "李小華"})
        self.assertEqual(out, "王O明（甲大學）｜學號 109○○○456 無法簽到；李O華也來信")
        self.assertEqual(hits, 2)

    def test_mask_report_text_whitelist_is_callers_job(self):
        # load_names 會先剔除 config 白名單；mask_report_text 只換傳進來的名字
        out, hits = mask_report_text("陳大文 回覆 王小明", {"王小明"})
        self.assertEqual(out, "陳大文 回覆 王O明")
        self.assertEqual(hits, 1)

    def test_mask_report_html_text_nodes_only(self):
        src = ('<style>.a{width:1234567890px}</style>'
               '<a href="https://x.example/1234567890123" title="王小明">王小明｜學號 109123456</a>'
               '<script>var id="A123456789";</script><p>手機 0912-345-678</p>')
        out, hits = mask_report_html(src, {"王小明"})
        self.assertEqual(out, '<style>.a{width:1234567890px}</style>'
                              '<a href="https://x.example/1234567890123" title="王小明">王O明｜學號 109○○○456</a>'
                              '<script>var id="A123456789";</script><p>手機 0912-○○○-○78</p>')
        self.assertEqual(hits, 1)

    def test_longer_name_replaced_before_shorter(self):
        out, _ = mask_report_html("<p>歐陽小明與陽小明</p>", {"歐陽小明", "陽小明"})
        self.assertEqual(out, "<p>歐陽OO與陽O明</p>")


class ConfigSwitch(unittest.TestCase):
    def test_default_on(self):
        import gmail_triage
        self.assertTrue(gmail_triage._DEFAULTS["mask_sensitive"])
        self.assertEqual(gmail_triage._DEFAULTS["mask_names_whitelist"], [])
        self.assertEqual(gmail_triage._DEFAULTS["mask_names_skip_domains"], [])


if __name__ == "__main__":
    unittest.main()
