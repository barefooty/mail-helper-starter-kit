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
- track：待辦追蹤——列出帶有手動標籤（名稱含 track_keywords）的信，
  依 last_from_us 分「待處理／看起來已完成」，is_old 標記非當年度；只讀取、絕不動標籤。

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
        "body_preview": body_preview(msg.get("payload", {})),
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
    print(json.dumps({
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": ADDR,
        "mode": "since_last_report" if cutoff else "window",
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

    print(json.dumps({
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": ADDR,
        "keywords": args.keywords,
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
    msg = MIMEText(body, "html" if args.html else "plain", "utf-8")
    msg["Subject"] = args.subject
    msg["From"] = formataddr((SENDER_NAME, ADDR))
    msg["To"] = ", ".join(args.to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    api("/messages/send", method="POST", body={"raw": raw})
    print(json.dumps({"ok": True, "to": args.to, "cc": cc, "subject": args.subject}, ensure_ascii=False))


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
    s.set_defaults(func=cmd_send)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
