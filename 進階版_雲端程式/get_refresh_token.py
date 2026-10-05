#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性工具：取得指定信箱的 Gmail API refresh token。

用法（在本機執行，會自動打開瀏覽器）：
  python get_refresh_token.py --client-id <用戶端ID> --client-secret <用戶端密碼> --hint <要掃描的信箱>

瀏覽器中請登入「要掃描的那個信箱」並允許授權
（會出現「Google 尚未驗證這個應用程式」警告 → 點「進階」→「前往 …（不安全）」，
這是自建憑證的正常現象）。完成後終端機會印出 GMAIL_REFRESH_TOKEN。
"""
import argparse
import http.server
import json
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

code_holder = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in qs:
            code_holder["code"] = qs["code"][0]
            self.wfile.write("<h2>授權完成，可以關閉這個視窗回到終端機。</h2>".encode())
        else:
            self.wfile.write(("<h2>授權失敗：%s</h2>" % qs.get("error", ["?"])[0]).encode())

    def log_message(self, *a):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", required=True)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--hint", default="", help="要掃描的信箱（登入畫面預選帳號用）")
    args = p.parse_args()

    redirect = "http://localhost:%d/" % args.port
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": args.hint,
    })
    print("若瀏覽器沒有自動開啟，請手動打開這個網址：\n\n" + url + "\n")
    webbrowser.open(url)

    server = http.server.HTTPServer(("localhost", args.port), Handler)
    while "code" not in code_holder:
        server.handle_request()

    data = urllib.parse.urlencode({
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "code": code_holder["code"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as r:
        tokens = json.load(r)

    rt = tokens.get("refresh_token")
    if not rt:
        print("沒拿到 refresh_token（回應：%s）。請確認網址參數含 prompt=consent 後重試。"
              % json.dumps(tokens))
        return
    print("成功！請把下面這行設為環境變數（注意保密）：\n")
    print("GMAIL_REFRESH_TOKEN=" + rt)


if __name__ == "__main__":
    main()
