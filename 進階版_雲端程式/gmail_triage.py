#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信箱分流小工具：透過 Gmail API（OAuth）掃描信箱、追蹤待辦、寄分流報告。

只用 Python 標準函式庫。需要環境變數：
  GMAIL_ADDRESS / GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN
信箱專屬設定（主旨前綴、追蹤關鍵字、標籤白名單、收件人白名單…）放同目錄 config.json；
缺檔時退回最保守的預設值（唯讀、不貼標籤）。

設計：
- 標籤原則：分類標籤全部由人手動管理，本工具**唯一**的例外是 mark 子指令——
  只能貼/撕 config.json 白名單內的標籤（auto_labels.enabled=true 才可用），
  其餘任何標籤一律不碰（進度也不靠標籤）。
- 「新信」＝比「最近一封已寄出的報告」（寄件備份中主旨以 report_subject_prefix 開頭者）更晚的信，
  外加重疊保險（寧可重列、不可漏信）。無標籤、無狀態檔，雲端/本機通用；
  若某場失敗沒寄出，下一場會自動涵蓋整段空窗。找不到任何已寄報告時退回 --days 時間窗。
  ★ 因此報告主旨必須以 report_subject_prefix 開頭，否則進度基準會失效。
- 每封信附 last_from_us（程式依 internalDate 精確計算：串中最後一封是否為我們寄出），
  「需要處理 vs 已處理」一律以此欄位為準，不做人為推斷。
- 出口打碼（2026-10-20 定版「先拆敏感資料、再讀信、再摘要」）：send 寄出前對報告全文再遮一次——
  fetch／track 看到的寄件人人名改「王O明」（.names_cache.json；config 的 mask_names_whitelist／
  mask_names_skip_domains 排除同事），號碼樣式同 mask_sensitive；HTML 只動文字節點。--no-mask 可關（除錯用）。
- track：待辦追蹤——列出帶有手動標籤（名稱含 track_keywords）的信，
  依 last_from_us 分「待處理／看起來已完成」，is_old 標記非當年度；只讀取、絕不動標籤。
- 敏感資料遮罩（config.json mask_sensitive，預設 true）：fetch／track 輸出的 body_preview
  先經 mask_sensitive() 把台灣身分證字號、手機、市話、10 碼以上的連續數字（信用卡／銀行帳號）、
  「帳號／密碼：」後面的值換成「○」；寄件人與主旨照原樣保留（報告端 prompt 另有第二道原則）。
  程式層只遮「已知樣式」，遮不到的個資靠報告端 prompt 自律，不要把這層當成完整的去識別。

子指令：
  fetch [--days N] [--max N] [--all]
      列出待分類的新信（收件匣＋垃圾信匣）。預設列「上一封報告之後」的信；
      --all 忽略報告基準、抓 --days 時間窗內全部信件（做每日/每週統計用）。
  track [--keywords K...] [--max N]
      待辦追蹤：列出手動標籤（名稱含關鍵字）下的信件與處理狀態（唯讀）。
  send --to <收件人>... --subject <主旨> --body-file <檔> [--html]
      寄出報告信（主旨開頭固定為 report_subject_prefix＝下一輪的進度基準）。
  mark --label <白名單標籤> --ids <id>...
      替信件貼白名單標籤（標籤不存在會自動建立；絕不動已讀狀態）。
      config.json 的 exclusive 可設互斥對（貼 A 自動撕 B）。
"""
import argparse
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime

ADDR = os.environ.get("GMAIL_ADDRESS", "")
CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
# ---------- 設定檔：信箱專屬值全部放 config.json ----------
# 與腳本同目錄的 config.json；缺檔或缺欄位時退回下方預設值（最保守：唯讀、不貼標籤）。
_DEFAULTS = {
    "report_subject_prefix": "【信箱報告】",   # 報告主旨固定開頭＝進度基準（不可改，改了基準失效）
    "sender_name": "信箱小幫手",              # 報告寄件顯示名稱
    "track_keywords": ["待處理"],             # 待辦追蹤：標籤名稱含這些關鍵字＝手動指派的待辦
    "auto_labels": {                          # mark 子指令；enabled=false 時 mark 直接報錯
        "enabled": False,
        "labels": [],                         # 唯一能動的標籤（白名單）
        "exclusive": {},
    },
    "allowed_recipients": [],  # send 收件人白名單（小寫比對）；空＝不限制（由 routine prompt 把關）
    "overlap_minutes": 15,     # 進度基準往前多抓幾分鐘：寧可重列、不可漏信
    "default_days": 5,         # fetch 找不到進度基準時退回的時間窗（天）
    "mask_sensitive": True,    # fetch/track 的 body_preview 先把身分證、電話、長數字、帳密值換成「○」
    "mask_names_whitelist": [],      # 出口打碼不改 O 的人名（承辦人、同事）
    "mask_names_skip_domains": [],   # 這些網域的寄件人視為同事、不改 O（如 my-school.edu.tw）
}


def _load_config():
    cfg = {k: v for k, v in _DEFAULTS.items()}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except ValueError as e:  # 不用 die()：它定義在本函式之後（模組載入順序）
            print("錯誤：config.json 不是合法 JSON：%s" % e, file=sys.stderr)
            sys.exit(1)
        for k, v in user.items():
            if not k.startswith("_"):  # _ 開頭的鍵當註解用，略過
                cfg[k] = v
    al = dict(_DEFAULTS["auto_labels"])
    al.update(cfg.get("auto_labels") or {})
    cfg["auto_labels"] = al
    return cfg


CFG = _load_config()
TRACK_KEYWORDS = list(CFG["track_keywords"])
MARK_ENABLED = bool(CFG["auto_labels"]["enabled"])
MARK_LABELS = list(CFG["auto_labels"]["labels"])
MARK_EXCLUSIVE = dict(CFG["auto_labels"]["exclusive"])
REPORT_SUBJECT = CFG["report_subject_prefix"]
SENDER_NAME = CFG["sender_name"]
ALLOWED_RECIPIENTS = [r.lower() for r in CFG["allowed_recipients"]]
OVERLAP_SEC = int(CFG["overlap_minutes"]) * 60
DEFAULT_DAYS = int(CFG["default_days"])
MASK_SENSITIVE = bool(CFG["mask_sensitive"])
MASK_NAMES_WHITELIST = set(CFG["mask_names_whitelist"])
MASK_NAMES_SKIP_DOMAINS = [d.lower() for d in CFG["mask_names_skip_domains"]]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))  # 台灣固定 UTC+8、無日光節約

_access_token = None


def die(msg):
    print("錯誤：" + msg, file=sys.stderr)
    sys.exit(1)


def get_access_token():
    global _access_token
    if _access_token:
        return _access_token
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        die("缺少 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN 環境變數")
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as r:
            _access_token = json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        die("換取 access token 失敗（refresh token 可能失效，需重跑 get_refresh_token.py）："
            + e.read().decode(errors="replace"))
    return _access_token


def api(path, method="GET", params=None, body=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + get_access_token(),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}  # batchModify 等回應為空
    except urllib.error.HTTPError as e:
        die("Gmail API %s %s 失敗：%s" % (method, path, e.read().decode(errors="replace")))


def header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ---------- 內文解析 ----------

def _decode_part(part):
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * ((4 - len(data) % 4) % 4))
    return raw.decode("utf-8", errors="replace")


def body_preview(payload, limit=1500):
    plain, html = [], []

    def walk(p):
        mime = p.get("mimeType", "")
        if mime.startswith("multipart/"):
            for c in p.get("parts", []):
                walk(c)
        elif mime == "text/plain":
            plain.append(_decode_part(p))
        elif mime == "text/html":
            html.append(_decode_part(p))

    walk(payload)
    text = "\n".join(t for t in plain if t.strip())
    if not text.strip() and html:
        import html as html_mod
        t = "\n".join(html)
        t = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        text = html_mod.unescape(t)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# ---------- 敏感資料遮罩（程式層，只遮已知樣式） ----------

MASK_CHAR = "○"

# 順序有意義：先抓最具體的樣式（身分證、手機、市話），再抓泛用的長數字，最後抓帳密值；
# 已換成「○」的部分不會再被後面的規則重複比對。
_RE_TW_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![A-Za-z0-9])")           # 台灣身分證：1 大寫字母＋1/2＋8 碼
_RE_MOBILE = re.compile(r"(?<![\d-])09\d{2}[- ]?\d{3}[- ]?\d{3}(?![\d-])")          # 手機：09 開頭 10 碼（可帶 - 或空格）
_RE_TEL = re.compile(                                                               # 市話：(0X)XXXX-XXXX、0X-XXX-XXXX、含分機
    r"(?<![\d-])(?:\(0\d{1,3}\)\s?|0\d{1,3}[- ])\d{3,4}[- ]?\d{3,4}"             # 要有括號或分隔符，純數字交給長數字規則
    r"(?:\s*(?:#|ext\.?|分機|轉)\s*\d{1,5})?(?![\d-])",
    re.I)
_RE_LONG_DIGITS = re.compile(r"(?<!\d)\d{10,}(?!\d)")                               # 連續 10 碼以上數字
_RE_CARD_GROUPS = re.compile(r"(?<!\d)\d{4}(?: \d{4}){3}(?!\d)")                     # 信用卡 4-4-4-4（空格分組）
_RE_HYPHEN_GROUPS = re.compile(r"(?<![\d-])\d+(?:-\d+)+(?![\d-])")                   # 連字號分組（銀行帳號 012-34-567890-1）
_RE_STUDENT_ID = re.compile(r"((?:學號|student ?(?:id|no\.?))\s*[:：=＝]?\s*)([A-Za-z]{0,2}\d{6,10})(?![A-Za-z0-9])", re.I)  # 「學號 109123456」只遮號碼
_RE_CRED = re.compile(                                                              # 「帳號／密碼：值」只遮值、保留標籤
    r"((?:密碼|通行碼|password|passwd|pwd|帳號|帳戶|account|login|username|user name)\s*[:：=＝]\s*)"
    r"([A-Za-z0-9][A-Za-z0-9@._\-!#$%^&*+=/\?]{2,})",
    re.I)


_RE_EMAIL = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")                                 # email：本地端 6 碼以上的數字串遮掉（學號型帳號），網域保留
_RE_ALNUM_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{8,9}(?![A-Za-z0-9])")              # 字母＋8～9 碼：s11564026、B11521055 這類學號（沒有「學號」標籤也遮）


def _mask_email_local(m):
    local, _, domain = m.group(0).partition("@")
    return re.sub(r"\d{6,}", lambda d: _partial(d.group(0)), local) + "@" + domain


def _blot(text):
    """全遮（備用）：整串換「○」。"""
    return MASK_CHAR * len(text)


def _secret(text):
    """密碼、通行碼：全遮（○○○○○○○○；2026-10-24 定：摘要上不得出現任何密碼內容，原 10-20 版留前兩字；定版）。密碼內容另由報告端規則禁止寫進摘要。"""
    return MASK_CHAR * len(text)


def _partial(text):
    """部分遮罩（2026-10-20 定版）：留頭尾、遮中間，看得出是哪一筆又不洩漏整串。
    只動英數字元，分隔符（- 空格 #）與「分機」等中文原樣。規則：英數 n 個 → 遮 n*4//10 個（至少 2）、
    尾巴留 3（奇數）或 2（偶數）、其餘留頭；14 個以上的純英數長串（信用卡）只留頭 4 尾 2。
    例：0912345678→0912○○○○78、A212345657→A212○○○○57、311123034→311○○○034、C115164117→C115○○○○17。"""
    idx = [i for i, c in enumerate(text) if c.isascii() and c.isalnum()]  # 只數英數，「分機」等中文不算
    n = len(idx)
    if n <= 3:
        return MASK_CHAR * len(text)
    if n >= 14 and text.isascii():
        head, tail = 4, 2                      # 信用卡這類純數字長串：只留頭 4 尾 2
    else:
        m = max(2, n * 4 // 10)
        tail = 3 if n % 2 else 2
        head = max(1, n - m - tail)
    chars = list(text)
    for k in idx[head:n - tail]:
        chars[k] = MASK_CHAR
    return "".join(chars)


def _mask_hyphen_groups(m):
    """連字號分組的數字：總位數 ≥ 10 才遮（避免日期 2026-10-16 這種 8 碼被誤殺）。"""
    s = m.group(0)
    return _partial(s) if sum(c.isdigit() for c in s) >= 10 else s


_RE_EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_RE_SECRET_LABEL = re.compile(r"密碼|通行碼|password|passwd|pwd", re.I)


def _mask_cred(m):
    """「帳號：xxx」的值若是完整 email 就不遮（email 是辨識寄件人用的，不算祕密）；
    「密碼：」後面的值一律遮，就算長得像 email（例：p@ss-word!）。"""
    label, value = m.group(1), m.group(2)
    nxt = m.string[m.end():m.end() + 1]
    if nxt in (MASK_CHAR, "@") and not _RE_SECRET_LABEL.search(label):
        return m.group(0)                   # 帳號值是 email（本地端已先被 email 規則遮過）：不再遮
    if _RE_EMAIL_VALUE.match(value) and not _RE_SECRET_LABEL.search(label):
        return m.group(0)
    if _RE_SECRET_LABEL.search(label):
        return label + _secret(value)    # 密碼、通行碼：全遮
    return label + _partial(value)      # 帳號：留頭尾


def mask_sensitive(text):
    """把文字中的已知個資樣式「留頭尾、遮中間」（密碼全遮），其餘原樣回傳。

    會遮：台灣身分證字號、手機（09 開頭 10 碼）、市話（含分機）、10 碼以上連續數字、
          信用卡 4-4-4-4、連字號分組且總位數 ≥10 的數字、「學號」後面的 6–10 碼、
          「帳號／密碼／password：」後面的值、字母＋8～9 碼的學號型代號（s11564026）、
          email 本地端 6 碼以上的數字串（413421259@school.edu.tw → ○○○○○○○○○@school.edu.tw，網域保留）。
    不遮：日期（2026-10-16、10/16）、時間（08:30）、金額（<10 碼）、課程代碼（GEM114A001）、
          沒有「學號」標籤的 9 碼以下純數字、沒有數字的 email。
    限制：只遮上述樣式；姓名、地址、自由文字中的敏感內容遮不到，報告端 prompt 仍須自律。
    """
    if not text:
        return text
    t = _RE_EMAIL.sub(_mask_email_local, text)          # 先處理 email：本地端數字遮、網域留
    t = _RE_TW_ID.sub(lambda m: _partial(m.group(0)), t)
    t = _RE_MOBILE.sub(lambda m: _partial(m.group(0)), t)
    t = _RE_TEL.sub(lambda m: _partial(m.group(0)), t)
    t = _RE_CARD_GROUPS.sub(lambda m: _partial(m.group(0)), t)
    t = _RE_LONG_DIGITS.sub(lambda m: _partial(m.group(0)), t)
    t = _RE_HYPHEN_GROUPS.sub(_mask_hyphen_groups, t)
    t = _RE_STUDENT_ID.sub(lambda m: m.group(1) + _partial(m.group(2)), t)
    t = _RE_CRED.sub(_mask_cred, t)
    t = _RE_ALNUM_ID.sub(lambda m: _partial(m.group(0)), t)
    return t


# ---------- 出口打碼（2026-10-20 定版：先拆敏感資料、再讀信、再摘要；寄出前再遮一次） ----------

NAMES_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".names_cache.json")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_RE_VIA = re.compile(r"\s*(?:經由|via)\s*\(.*$")
_NOT_NAME_ENDINGS = ("中心", "大學", "學院", "學校", "科大", "系", "所", "處", "室", "組", "部", "公司", "團隊", "小組",
                     "辦公室", "教育", "平台", "通知", "客服", "基金會", "協會", "學會", "書院", "委員會", "計畫",
                     "小幫手", "信箱", "表單", "系統", "官方", "團", "老師", "助教", "同學", "軟體", "服務", "工作室",
                     "科技", "行銷", "網", "站", "院", "部門", "股份", "有限", "設計", "數位", "顧問", "報", "會")
_COMPOUND_SURNAMES = ("歐陽", "司馬", "司徒", "諸葛", "上官", "夏侯", "范姜", "張簡", "長孫", "東方", "皇甫", "尉遲", "公孫")


def _valid_name(seg):
    """2～4 個中文字；4 字只接受複姓開頭（台灣 4 字名幾乎都是複姓，其餘多半是單位名）；不以單位字結尾。"""
    if not seg or not (2 <= len(seg) <= 4) or not _RE_CJK_RUN.fullmatch(seg):
        return False
    if any(seg.endswith(w) for w in _NOT_NAME_ENDINGS):
        return False
    if len(seg) == 4 and not seg.startswith(_COMPOUND_SURNAMES):
        return False
    return True


def name_from_display(display):
    """從寄件人顯示名抓「人名」候選（2～4 個中文字）；單位名、系統名回傳 None。
    「四視光一A王小明」「人力資源發展系李小華」這類帶系級前綴的取最後 3 字；
    兩字候選只有在顯示名就是那兩個字時才算人名（避免「demo學網」這類平台名被當人名）。"""
    if not display:
        return None
    d = _RE_VIA.sub("", display).strip(' "\'')
    runs = _RE_CJK_RUN.findall(d)
    if not runs:
        return None
    seg = runs[-1]
    if any(seg.endswith(w) for w in _NOT_NAME_ENDINGS):
        return None
    if len(seg) > 4:
        seg = seg[-3:]
    if len(seg) == 2 and d != seg:
        return None
    return seg if _valid_name(seg) else None


def _display_and_domain(from_header):
    m = re.match(r'\s*"?(.*?)"?\s*<([^>]+)>\s*$', from_header or "")
    if m:
        disp, addr = m.group(1), m.group(2)
    else:
        disp, addr = "", (from_header or "").strip()
    dom = addr.split("@")[-1].lower() if "@" in addr else ""
    return disp, dom


def _update_names_cache(records):
    """fetch／track 後把這輪看到的寄件人人名記進 .names_cache.json，給 send 的出口打碼用。
    同一場 routine 的 fetch→track→send 在同一個工作目錄（雲端沙箱也是），本機檔已 gitignore。
    config 的 mask_names_skip_domains（同事網域）與 mask_names_whitelist（同事姓名）不記。"""
    if not MASK_SENSITIVE:
        return
    cache = {}
    if os.path.exists(NAMES_CACHE):
        try:
            with open(NAMES_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except ValueError:
            cache = {}
    cache = {k: v for k, v in cache.items() if _valid_name(k)}  # 規則更新時順手清掉舊的誤判
    now = int(time.time())
    for r in records:
        froms = [r.get("from", "")] + [s.get("from", "") for s in r.get("thread_siblings", [])]
        for fh in froms:
            disp, dom = _display_and_domain(fh)
            if dom and any(dom == d or dom.endswith("." + d) for d in MASK_NAMES_SKIP_DOMAINS):
                continue
            n = name_from_display(disp)
            if n and n not in MASK_NAMES_WHITELIST:
                cache[n] = now
    if len(cache) > 800:  # 只留最近看到的
        cache = dict(sorted(cache.items(), key=lambda kv: -kv[1])[:800])
    with open(NAMES_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def load_names(extra_file=None):
    names = set()
    for path in (NAMES_CACHE, extra_file):
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                names.update(data.keys() if isinstance(data, dict) else data)
            except ValueError:
                pass
    return {n for n in names if _valid_name(n) and n not in MASK_NAMES_WHITELIST}


def name_mask(name):
    """王小明→王O明、陳明→陳O、歐陽小明→歐陽OO（2026-10-16 定版的列法）。"""
    n = len(name)
    if n <= 1:
        return name
    if n == 2:
        return name[0] + "O"
    if n == 3:
        return name[0] + "O" + name[2]
    return name[:2] + "O" * (n - 2)


def mask_report_text(text, names):
    """報告文字：先把已知人名改 O（長的先換），再跑 mask_sensitive。回傳 (文字, 人名命中數)。"""
    out, hits = text, 0
    for n in sorted(names, key=len, reverse=True):
        if n in out:
            hits += out.count(n)
            out = out.replace(n, name_mask(n))
    return mask_sensitive(out), hits


_RE_HTML_SPLIT = re.compile(r"(<style[^>]*>.*?</style>|<script[^>]*>.*?</script>|<[^>]+>)", re.S | re.I)


def mask_report_html(html_text, names):
    """只動 HTML 的文字節點；標籤、屬性、<style>／<script> 原樣（連結不會被「○」弄壞）。"""
    parts = _RE_HTML_SPLIT.split(html_text)
    hits = 0
    for i, p in enumerate(parts):
        if not p or p.startswith("<"):
            continue
        parts[i], h = mask_report_text(p, names)
        hits += h
    return "".join(parts), hits


def thread_info(tid, cache):
    """回傳 (sibs, replied_by_us, last_from_us, last_from)。依 internalDate 精確排序。"""
    if tid in cache:
        return cache[tid]
    thr = api("/threads/%s" % tid, params=[
        ("format", "metadata"),
        ("metadataHeaders", "From"),
        ("metadataHeaders", "Subject"),
        ("metadataHeaders", "Date"),
    ])
    msgs = sorted(thr.get("messages", []), key=lambda x: int(x.get("internalDate", 0)))
    sibs = []
    replied = False
    for t in msgs:
        th = t.get("payload", {}).get("headers", [])
        frm = header(th, "From")
        from_us = "SENT" in t.get("labelIds", []) or (ADDR and ADDR.lower() in frm.lower())
        if from_us:
            replied = True
        sibs.append({
            "id": t["id"],
            "from": frm,
            "subject": header(th, "Subject"),
            "date": header(th, "Date"),
            "from_us": from_us,
        })
    last_from_us = bool(sibs) and sibs[-1]["from_us"]
    last_from = sibs[-1]["from"] if sibs else ""
    cache[tid] = (sibs, replied, last_from_us, last_from)
    return cache[tid]


# ---------- fetch ----------

def last_report_ts():
    """回傳最近一封已寄出報告（主旨以 REPORT_SUBJECT 開頭）的 internalDate 秒數；沒有則 None。
    這就是進度基準——不需要標籤、不需要狀態檔。"""
    resp = api("/messages", params={
        "q": 'in:sent subject:"%s"' % REPORT_SUBJECT,
        "maxResults": 1,
    })
    msgs = resp.get("messages", [])
    if not msgs:
        return None
    msg = api("/messages/%s" % msgs[0]["id"], params={"format": "minimal"})
    return int(msg.get("internalDate", 0)) // 1000


def message_record(msg, scope, id2name, cache):
    """把 Gmail API 的 message（format=full）整理成報告用的 dict。"""
    headers = msg.get("payload", {}).get("headers", [])
    tid = msg.get("threadId")
    sibs, replied, last_from_us, last_from = thread_info(tid, cache)
    return {
        "id": msg["id"],
        "thread_id": tid,
        "folder_type": scope,
        "from": header(headers, "From"),
        "to": header(headers, "To"),
        "subject": header(headers, "Subject"),
        "date": header(headers, "Date"),
        "internal_ts": int(msg.get("internalDate", 0)) // 1000,
        "unread": "UNREAD" in msg.get("labelIds", []),
        "labels": [id2name.get(l, l) for l in msg.get("labelIds", [])],
        "replied_by_us": replied,
        "last_from_us": last_from_us,      # ★ 串中最後一封是我們寄的（=已處理）
        "last_msg_from": last_from,
        "thread_siblings": [s for s in sibs if s["id"] != msg["id"]],
        # ★ 程式層打碼：mask_sensitive=true（預設）時先遮已知個資樣式；主旨與寄件人不動
        "body_preview": (mask_sensitive(body_preview(msg.get("payload", {})))
                         if MASK_SENSITIVE else body_preview(msg.get("payload", {}))),
    }


def cmd_fetch(args):
    now_ts = server_now_utc().timestamp()
    cutoff = None
    if not args.all:
        ts = last_report_ts()
        if ts:
            cutoff = ts - OVERLAP_SEC  # 重疊 15 分鐘：寧可重列、不可漏信
    if cutoff:
        days = max(args.days, int((now_ts - cutoff) // 86400) + 2)  # 時間窗自動涵蓋整段空窗
    else:
        days = args.days
    qtime = "newer_than:%dd" % days

    found = {}
    for scope, q in (("inbox", "in:inbox"), ("spam", "in:spam")):
        resp = api("/messages", params={
            "q": "%s %s" % (q, qtime),
            "maxResults": args.max,
            "includeSpamTrash": "true",
        })
        for m in resp.get("messages", []):
            if m["id"] not in found:
                found[m["id"]] = scope

    labels = api("/labels")["labels"]
    id2name = {l["id"]: l["name"] for l in labels}

    messages = []
    cache = {}
    for mid, scope in found.items():
        msg = api("/messages/%s" % mid, params={"format": "full"})
        if cutoff and int(msg.get("internalDate", 0)) // 1000 <= cutoff:
            continue  # 上一封報告（含重疊窗）之前的信＝已報告過
        messages.append(message_record(msg, scope, id2name, cache))

    messages.sort(key=lambda x: x["internal_ts"], reverse=True)
    _update_names_cache(messages)  # 出口打碼用的人名清單
    print(json.dumps({
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": ADDR,
        "mode": "since_last_report" if cutoff else "window",
        "mask_sensitive": MASK_SENSITIVE,  # body_preview 是否已做程式層打碼
        "cutoff_taipei": (datetime.datetime.fromtimestamp(cutoff, TAIPEI)
                          .strftime("%Y-%m-%d %H:%M") if cutoff else None),
        "count": len(messages),
        "pending_action": sum(1 for m in messages if not m["last_from_us"]),
        "messages": messages,
    }, ensure_ascii=False, indent=1))


# ---------- track（待辦追蹤，唯讀） ----------

def cmd_track(args):
    """列出帶手動待辦標籤（名稱含關鍵字）的信件；last_from_us=True 視為「看起來已完成」。
    只讀取，絕不新增/移除任何標籤。"""
    labels = api("/labels")["labels"]
    id2name = {l["id"]: l["name"] for l in labels}
    tracked_labels = [l for l in labels
                      if l.get("type") == "user"
                      and any(k in l["name"] for k in args.keywords)]

    this_year = server_now_utc().astimezone(TAIPEI).year
    cache = {}
    groups = []
    for l in sorted(tracked_labels, key=lambda x: x["name"]):
        resp = api("/messages", params={"labelIds": l["id"], "maxResults": args.max,
                                        "includeSpamTrash": "true"})
        msgs = []
        for m in resp.get("messages", []):
            msg = api("/messages/%s" % m["id"], params={"format": "full"})
            rec = message_record(msg, "labeled", id2name, cache)
            rec["tracked_label"] = l["name"]
            # 「舊」＝非當年度：信件時間不在今年（報告端另可依內容加註）
            ts_year = datetime.datetime.fromtimestamp(rec["internal_ts"], TAIPEI).year
            rec["is_old"] = ts_year < this_year
            msgs.append(rec)
        msgs.sort(key=lambda x: x["internal_ts"], reverse=True)
        groups.append({
            "label": l["name"],
            "total": len(msgs),
            "pending": sum(1 for m in msgs if not m["last_from_us"]),
            "looks_done": sum(1 for m in msgs if m["last_from_us"]),
            "messages": msgs,
        })

    _update_names_cache([m for grp in groups for m in grp["messages"]])  # 出口打碼用的人名清單
    print(json.dumps({
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": ADDR,
        "keywords": args.keywords,
        "mask_sensitive": MASK_SENSITIVE,
        "labels_matched": [g["label"] for g in groups],
        "pending_total": sum(g["pending"] for g in groups),
        "groups": groups,
    }, ensure_ascii=False, indent=1))


# ---------- mark（白名單標籤） ----------

def _get_or_create_label(name):
    """回傳白名單標籤的 id；不存在就建立（只允許 MARK_LABELS 內的名稱）。"""
    for l in api("/labels")["labels"]:
        if l["name"] == name:
            return l["id"]
    created = api("/labels", method="POST", body={
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    })
    return created["id"]


def cmd_mark(args):
    """貼白名單標籤到指定信件。安全設計：
    - 只能貼 MARK_LABELS 三個標籤，其他名稱直接報錯（保護團隊手動標籤）。
    - 只會移除互斥的另一個 claude 狀態標籤，絕不動任何其他標籤。
    """
    if not MARK_ENABLED:
        die("此信箱未啟用自動標籤（config.json 的 auto_labels.enabled=false）")
    if args.label not in MARK_LABELS:
        die("mark 只允許這些標籤：%s（收到：%s）" % ("、".join(MARK_LABELS), args.label))
    add_id = _get_or_create_label(args.label)
    body = {"ids": args.ids, "addLabelIds": [add_id]}
    other = MARK_EXCLUSIVE.get(args.label)
    if other:
        # 互斥標籤存在才需要撕（batchModify 移除不存在的標籤也安全，但避免無謂建立）
        other_id = next((l["id"] for l in api("/labels")["labels"] if l["name"] == other), None)
        if other_id:
            body["removeLabelIds"] = [other_id]
    api("/messages/batchModify", method="POST", body=body)
    print(json.dumps({"ok": True, "label": args.label, "ids": args.ids,
                      "removed_exclusive": other if body.get("removeLabelIds") else None},
                     ensure_ascii=False))


# ---------- send ----------

def cmd_send(args):
    cc = args.cc or []
    if ALLOWED_RECIPIENTS:
        bad = [t for t in (args.to + cc) if t.lower() not in ALLOWED_RECIPIENTS]
        if bad:
            die("收件人不在 config.json 的 allowed_recipients 白名單：%s" % "、".join(bad))
    with open(args.body_file, encoding="utf-8") as f:
        body = f.read()
    stats = {"enabled": False}
    if MASK_SENSITIVE and not args.no_mask:  # ★ 出口打碼：人名改 O＋號碼再掃一次，只動文字節點
        names = load_names(args.names_file)
        body, hits = (mask_report_html if args.html else mask_report_text)(body, names)
        stats = {"enabled": True, "names_known": len(names), "name_hits": hits}
        with open(args.body_file + ".masked", "w", encoding="utf-8") as f:  # 留一份寄出的實際內容供核對
            f.write(body)
    msg = MIMEText(body, "html" if args.html else "plain", "utf-8")
    msg["Subject"] = args.subject
    msg["From"] = formataddr((SENDER_NAME, ADDR))
    msg["To"] = ", ".join(args.to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    api("/messages/send", method="POST", body={"raw": raw})
    print(json.dumps({"ok": True, "to": args.to, "cc": cc, "subject": args.subject, "masked": stats}, ensure_ascii=False))


def server_now_utc():
    """回傳 Google 伺服器當下時間（UTC, tz-aware）。
    來源＝任一 Gmail API 回應的 HTTP `Date` 標頭，完全不受本機時鐘/時區影響。"""
    req = urllib.request.Request(API + "/profile", headers={
        "Authorization": "Bearer " + get_access_token(),
    })
    try:
        with urllib.request.urlopen(req) as r:
            d = r.headers.get("Date")
    except urllib.error.HTTPError as e:
        die("讀取伺服器時間失敗：" + e.read().decode(errors="replace"))
    if not d:
        die("Gmail 回應缺少 Date 標頭，無法取得伺服器時間")
    return parsedate_to_datetime(d).astimezone(datetime.timezone.utc)


def cmd_now(args):
    """以伺服器時間為準，算出台灣日期／星期／場次。報告日期一律用此輸出，勿信本機 date。"""
    server = server_now_utc()
    tw = server.astimezone(TAIPEI)
    weekday = "一二三四五六日"[tw.weekday()]
    if tw.hour < 12:
        session, sched_hour = "08:00場", 8
    else:
        session, sched_hour = "15:00場", 15
    late = (tw.hour * 60 + tw.minute) > sched_hour * 60 + 20  # 超過排定時間 20 分鐘＝補發
    if late:
        session += "（補發）"
    local = datetime.datetime.now(datetime.timezone.utc)
    skew = round((local - server).total_seconds())
    print(json.dumps({
        "server_utc": server.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "taipei": tw.strftime("%Y-%m-%d %H:%M"),
        "date": tw.strftime("%Y-%m-%d"),
        "weekday": weekday,
        "session": session,
        "is_late_makeup": late,
        "local_clock_skew_sec": skew,  # 本機時鐘與伺服器差幾秒（>60 代表本機不可信）
    }, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help="往回抓幾天（預設 %d，可由 config.json default_days 調整）" % DEFAULT_DAYS)
    f.add_argument("--max", type=int, default=100, help="每個範圍最多抓幾封（預設 100）")
    f.add_argument("--all", action="store_true", help="忽略進度標籤，抓時間窗內全部（統計用）")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("track")
    t.add_argument("--keywords", nargs="+", default=TRACK_KEYWORDS,
                   help="標籤名稱關鍵字（預設：%s）" % "、".join(TRACK_KEYWORDS))
    t.add_argument("--max", type=int, default=100, help="每個標籤最多列幾封（預設 100）")
    t.set_defaults(func=cmd_track)

    nw = sub.add_parser("now")
    nw.set_defaults(func=cmd_now)

    mk = sub.add_parser("mark")
    mk.add_argument("--label", required=True,
                    help="要貼的標籤（僅限：%s）" % "、".join(MARK_LABELS))
    mk.add_argument("--ids", nargs="+", required=True, help="要貼標籤的信件 id（可多個）")
    mk.set_defaults(func=cmd_mark)

    s = sub.add_parser("send")
    s.add_argument("--to", action="append", required=True)
    s.add_argument("--cc", action="append", help="副本收件人（可多個；同受 allowed_recipients 白名單限制）")
    s.add_argument("--subject", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--html", action="store_true")
    s.add_argument("--no-mask", action="store_true", help="不做出口打碼（除錯用；正式 routine 不要加）")
    s.add_argument("--names-file", help="額外的人名清單 JSON（list 或 {name: ts}），與 .names_cache.json 合併")
    s.set_defaults(func=cmd_send)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
