# -*- coding: utf-8 -*-
"""讀回《工作脈絡問卷.docx》，輸出 Markdown 給精靈判讀。

用法：
    python read_questionnaire.py 我的工作脈絡/工作脈絡問卷.docx [--out 讀回結果.md]

規則：
    - 依段落開頭「Q<數字>｜」切題（make_questionnaire.py 產的錨點；使用者改了標題文字也沒關係，只認「Q數字｜」）。
    - 勾選題：☑ ■ ✓ ✔ V v 是 打在選項前或後都算勾；☐ 或沒記號＝沒勾。
    - 表格：整列空白就略過；答案框（1×1 表格）空白印「（未填）」。
    - 最後附「讀回統計」：哪幾題有答案、哪幾題空白，給精靈決定只追問什麼。
"""
import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

ANCHOR = re.compile(r"^\s*Q(\d+)[｜|:：]\s*(.*)$")
EMPTY_MARK = re.compile(r"^[\s＿_\-—–]*$")


def iter_blocks(doc):
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif child.tag.endswith("}tbl"):
            yield Table(child, doc)


def cell_str(cell):
    return "\n".join(p.text.strip() for p in cell.paragraphs if p.text.strip()).strip()


def is_blank(s):
    return not s or EMPTY_MARK.match(s) is not None


def only_placeholder(cell):
    """答案框裡的佔位字是灰斜體；若所有非空 run 都是斜體，視為使用者沒填。"""
    runs = [r for p in cell.paragraphs for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.font.italic for r in runs)


def render_table(tbl):
    rows = [[cell_str(c) for c in r.cells] for r in tbl.rows]
    if not rows:
        return "", False
    # 答案框：1×1
    if len(rows) == 1 and len(rows[0]) == 1:
        v = rows[0][0]
        if is_blank(v) or only_placeholder(tbl.cell(0, 0)):
            return "> （未填）", False
        return "> " + v.replace("\n", "  \n> "), True
    header, data = rows[0], rows[1:]
    kept = [r for r in data if any(not is_blank(x) for x in r)]
    if not kept:
        return f"| {' | '.join(header)} |\n|{'---|' * len(header)}\n| （未填） |", False
    out = [f"| {' | '.join(header)} |", "|" + "---|" * len(header)]
    for r in kept:
        out.append("| " + " | ".join((x or "（空）").replace("\n", " ") for x in r) + " |")
    return "\n".join(out), True


def render_option(text):
    """「☐ 選項」→ 未勾；「☑ 選項」「■ 選項」「☐ 選項 V」「☐ 選項 是」→ 已勾。"""
    t = text.strip()
    lead = t[0] if t and t[0] in "☐☑■✓✔" else ""
    body = t[1:].strip() if lead else t
    tail = re.search(r"[☑■✓✔Vv]\s*$|\s是\s*$", body)
    checked = lead in ("☑", "■", "✓", "✔") or tail is not None
    if tail:
        body = body[: tail.start()].rstrip()
    return f"- [{'x' if checked else ' '}] {body}", checked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("docx")
    ap.add_argument("--out")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    doc = Document(a.docx)
    sections = []  # (qno, title, lines, answered)
    cur = None
    preface = []
    for blk in iter_blocks(doc):
        if isinstance(blk, Paragraph):
            txt = blk.text.strip()
            m = ANCHOR.match(txt)
            if m:
                cur = {"q": int(m.group(1)), "title": re.sub(r"［.*?］", "", m.group(2)).strip(), "lines": [], "answered": False, "checked": 0}
                sections.append(cur)
                continue
            if not txt:
                continue
            if cur is None:
                preface.append(txt)
                continue
            if re.match(r"^[☐☑■✓✔]", txt):
                line, ck = render_option(txt)
                cur["lines"].append(line)
                if ck:
                    cur["answered"] = True
                    cur["checked"] += 1
            else:
                # 題目說明（灰字提示）照印但縮排，方便精靈對照
                cur["lines"].append(f"_{txt}_")
        else:
            if cur is None:
                continue
            md, filled = render_table(blk)
            cur["lines"].append(md)
            if filled:
                cur["answered"] = True

    out = ["# 問卷讀回結果", ""]
    if preface:
        out.append("> " + " / ".join(preface[:3]))
        out.append("")
    for s in sections:
        out.append(f"## Q{s['q']}｜{s['title']}")
        out.extend(s["lines"])
        out.append("")
    answered = [f"Q{s['q']}" for s in sections if s["answered"]]
    blank = [f"Q{s['q']}" for s in sections if not s["answered"]]
    out.append("## 讀回統計")
    out.append(f"- 有答案：{len(answered)} 題（{'、'.join(answered) or '無'}）")
    out.append(f"- 空白：{len(blank)} 題（{'、'.join(blank) or '無'}）")
    out.append("- 提醒精靈：只追問空白的必答題（Q1、2、3、5、6、7、8、9）與前後矛盾處；選填題空白用預設並在確認清單標「用預設，之後可補」。")
    text = "\n".join(out)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"已寫入：{a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
