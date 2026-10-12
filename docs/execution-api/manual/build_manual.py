"""Build the versioned interface manual from its complete Markdown source."""

import argparse
import hashlib
import re
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.ttLib import TTFont as FontToolsFont
from fontTools.varLib.instancer import instantiateVariableFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, HRFlowable, KeepTogether,
    PageBreak, PageTemplate, Paragraph, Preformatted, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

INK = colors.HexColor("#202327")
GRAY = colors.HexColor("#666B71")
LINE = colors.HexColor("#D6D9DD")
PALE = colors.HexColor("#F5F6F7")
WIDTH = A4[0] - 44 * mm


def load_fonts(variable_font, mono_font, cache):
    cache.mkdir(parents=True, exist_ok=True)
    font_digest = hashlib.sha256(variable_font.read_bytes()).hexdigest()[:12]
    for name, weight in (("Body", 400), ("Heading", 600)):
        target = cache / f"CJK-{font_digest}-{weight}.ttf"
        if not target.exists():
            font = FontToolsFont(variable_font)
            if "fvar" in font:
                font = instantiateVariableFont(font, {"wght": weight}, inplace=True)
            font.save(target)
        pdfmetrics.registerFont(TTFont(name, str(target)))
    pdfmetrics.registerFont(TTFont("Code", str(mono_font)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Heading")


def styles():
    return {
        "body": ParagraphStyle("body", fontName="Body", fontSize=10,
                               leading=16, textColor=INK, spaceAfter=6,
                               wordWrap="CJK"),
        "h1": ParagraphStyle("h1", fontName="Heading", fontSize=18,
                             leading=26, textColor=INK, spaceAfter=17,
                             keepWithNext=True),
        "h2": ParagraphStyle("h2", fontName="Heading", fontSize=11.5,
                             leading=18, textColor=INK, spaceBefore=10,
                             spaceAfter=7, keepWithNext=True),
        "bullet": ParagraphStyle("bullet", fontName="Body", fontSize=10,
                                 leading=16, textColor=INK, leftIndent=10,
                                 firstLineIndent=-10, spaceAfter=4, wordWrap="CJK"),
        "cell": ParagraphStyle("cell", fontName="Body", fontSize=9.2,
                               leading=13, textColor=INK, wordWrap="CJK"),
        "cellhead": ParagraphStyle("cellhead", fontName="Heading", fontSize=9.2,
                                   leading=13, textColor=INK, wordWrap="CJK"),
        "note": ParagraphStyle("note", fontName="Body", fontSize=9.2,
                               leading=15, textColor=GRAY, leftIndent=9,
                               rightIndent=9, borderColor=LINE, borderWidth=.5,
                               borderPadding=9, backColor=PALE, spaceBefore=9,
                               spaceAfter=9, wordWrap="CJK"),
        "code": ParagraphStyle("code", fontName="Code", fontSize=8.3,
                               leading=11, textColor=INK, spaceAfter=12),
        "toc": ParagraphStyle("toc", fontName="Body", fontSize=11,
                              leading=27, textColor=INK, leftIndent=0,
                              firstLineIndent=0, rightIndent=20),
    }


def inline(text):
    parts = re.split(r"(`[^`]+`)", text)
    result = "".join(
        '<font name="Code" size="8.6">' + escape(part[1:-1]) + "</font>"
        if part.startswith("`") else escape(part) for part in parts
    )
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)


def make_table(rows, style):
    columns = len(rows[0])
    if columns == 3:
        widths = [17 * mm, 103 * mm, WIDTH - 120 * mm] if rows[0][0] == "方法" else [29 * mm, 91 * mm, WIDTH - 120 * mm]
    else:
        first_width = 62 * mm if rows[0][0] == "错误码" else 47 * mm
        widths = [first_width, WIDTH - first_width]
    cells = [[Paragraph(inline(cell), style["cellhead" if index == 0 else "cell"])
              for cell in row] for index, row in enumerate(rows)]
    table = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), PALE),
        ("LINEABOVE", (0, 0), (-1, 0), .7, INK),
        ("LINEBELOW", (0, 0), (-1, 0), .6, INK),
        ("LINEBELOW", (0, 1), (-1, -1), .35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [table, Spacer(1, 9)]


class Diagram(Flowable):
    def __init__(self, kind):
        super().__init__()
        self.kind, self.width = kind, WIDTH
        self.height = 86 if kind == "workflow" else 205

    def draw(self):
        c = self.canv
        c.setStrokeColor(LINE)
        c.setFillColor(INK)

        def box(x, y, width, text):
            c.setFillColor(PALE)
            c.rect(x, y, width, 32, fill=1, stroke=1)
            c.setFillColor(INK)
            c.setFont("Body" if self.kind == "workflow" else "Code", 9.3)
            c.drawCentredString(x + width / 2, y + 11, text)

        def arrow(x1, y1, x2, y2):
            c.setStrokeColor(GRAY)
            c.line(x1, y1, x2, y2)
            c.line(x2, y2, x2 - 3, y2 + 5)
            c.line(x2, y2, x2 + 3, y2 + 5)

        if self.kind == "workflow":
            bw = 136
            gap = (WIDTH - 3 * bw) / 2
            for index, text in enumerate(("上游编排系统", "执行网关", "Dobot 设备适配器")):
                x = index * (bw + gap)
                box(x, 46, bw, text)
                if index < 2:
                    c.line(x + bw, 62, x + bw + gap, 62)
            c.setFont("Body", 8.7)
            c.setFillColor(GRAY)
            c.drawString(0, 21, "运行包提交与校验 → 技能展开与执行 → 状态查询与事件回传")
        else:
            left, right, bw = 45, WIDTH - 157, 112
            for x, y, text in ((left, 157, "pick-A"), (right, 157, "pick-B"),
                               (left, 105, "hold-A"), (right, 105, "place-B"),
                               (left, 53, "place-A"), (right, 53, "repick-B"),
                               (right, 1, "return-B")):
                box(x, y, bw, text)
            arrow(left + bw / 2, 157, left + bw / 2, 137)
            arrow(left + bw / 2, 105, left + bw / 2, 85)
            arrow(right + bw / 2, 157, right + bw / 2, 137)
            arrow(right + bw / 2, 105, right + bw / 2, 85)
            arrow(right + bw / 2, 53, right + bw / 2, 33)
            c.line(left + bw, 69, right, 69)
            c.line(right, 69, right - 5, 72)
            c.line(right, 69, right - 5, 66)
            c.setFont("Code", 8)
            c.setFillColor(GRAY)
            c.drawCentredString((left + bw + right) / 2, 82, "AND")


class ManualDoc(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "h1":
            text = flowable.getPlainText()
            if re.match(r"^\d{2} ", text):
                key = "chapter-" + text[:2]
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(text, key, 0)
                self.notify("TOCEntry", (0, text, self.page, key))


def on_page(canvas, doc):
    if doc.page == 1:
        return
    canvas.saveState()
    canvas.setFont("Body", 8)
    canvas.setFillColor(GRAY)
    canvas.drawString(22 * mm, A4[1] - 17 * mm, "Dobot 原子技能执行网关")
    canvas.setFont("Code", 7.5)
    canvas.drawRightString(A4[0] - 22 * mm, A4[1] - 17 * mm, "DBT-GW-INT-001 / V1.1")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(.45)
    canvas.line(22 * mm, A4[1] - 21 * mm, A4[0] - 22 * mm, A4[1] - 21 * mm)
    canvas.line(22 * mm, 18 * mm, A4[0] - 22 * mm, 18 * mm)
    canvas.setFont("Body", 7.5)
    canvas.drawString(22 * mm, 13 * mm, "接口与联调手册  ·  2026-10-12")
    canvas.setFont("Code", 8)
    canvas.drawRightString(A4[0] - 22 * mm, 13 * mm, f"{doc.page:02d}")
    canvas.restoreState()


def code_block(text, style):
    if any(pdfmetrics.stringWidth(line, "Code", 8.3) > WIDTH - 20 for line in text.splitlines()):
        raise ValueError("Code line exceeds available width; wrap the source line")
    flow = Preformatted(text, style["code"])
    block = Table([[flow]], colWidths=[WIDTH], hAlign="LEFT")
    block.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE),
        ("BOX", (0, 0), (-1, -1), .35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [KeepTogether([block]), Spacer(1, 10)]


def build(source, output, variable_font, mono_font):
    output.parent.mkdir(parents=True, exist_ok=True)
    load_fonts(variable_font, mono_font, output.parent / ".font-cache")
    style = styles()
    text = source.read_text(encoding="utf-8-sig")
    story = [Spacer(1, 38 * mm)]
    cover_label = ParagraphStyle("coverlabel", fontName="Code", fontSize=10,
                                leading=16, textColor=GRAY, spaceAfter=18)
    cover_title = ParagraphStyle("covertitle", fontName="Heading", fontSize=28,
                                leading=42, textColor=INK, spaceAfter=9)
    cover_sub = ParagraphStyle("coversub", fontName="Body", fontSize=17,
                              leading=27, textColor=GRAY)
    story.extend([
        Paragraph("DOBOT / EXECUTION GATEWAY", cover_label),
        Paragraph("原子技能执行网关", cover_title),
        Paragraph("接口与联调手册", cover_sub),
        Spacer(1, 16 * mm),
        HRFlowable(width=WIDTH, thickness=1, color=INK),
        Spacer(1, 12 * mm),
    ])
    for line in ("文档编号  DBT-GW-INT-001", "文档版本  1.1", "发布日期  2026-10-12",
                 "适用配置  dobot-dag/1", "实现基线  3eed0a9"):
        story.append(Paragraph(line, style["body"]))
    story.extend([Spacer(1, 29 * mm), Paragraph(
        "适用于上游编排系统与 Dobot X-Trainer 双 Nova 设备侧集成。<br/>"
        "包含接口约定、运行包示例、执行管理、联调流程与验收要求。", style["body"]), PageBreak()])
    story.append(Paragraph("目录与阅读说明", style["h1"]))
    toc = TableOfContents()
    toc.levelStyles = [style["toc"]]
    toc.dotsMinLevel = 0
    story.extend([toc, Spacer(1, 24), Paragraph(
        "接入开发先阅读第 02 至 08 章；联调人员按第 09 至 12 章执行。"
        "所有命令在仓库根目录运行；报文示例中的任务标识和令牌为占位值。", style["body"]),
        Paragraph("修订说明", style["h2"]), Paragraph(
            "1.1：规范角色与术语，重组接口和验收内容，补充完整单臂示例；"
            "更新字体、表格、代码与图示排版。接口实现及机器人控制行为未变更。", style["body"]), PageBreak()])
    lines = text.split("<!-- page -->", 1)[1].strip().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line == "<!-- page -->":
            story.append(PageBreak())
        elif line.startswith("<!-- diagram:"):
            story.extend([Diagram(line.split(":", 1)[1].replace("-->", "").strip()), Spacer(1, 12)])
        elif line.startswith("# "):
            story.append(Paragraph(inline(line[2:]), style["h1"]))
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), style["h2"]))
        elif line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError("Unclosed code fence")
            story.extend(code_block("\n".join(code), style))
        elif line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                row = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in row):
                    rows.append(row)
                index += 1
            story.extend(make_table(rows, style))
            continue
        elif line.startswith("> "):
            story.append(Paragraph(inline(line[2:]), style["note"]))
        elif line.startswith("- "):
            story.append(Paragraph("• " + inline(line[2:]), style["bullet"]))
        else:
            story.append(Paragraph(inline(line), style["body"]))
        index += 1
    doc = ManualDoc(str(output), pagesize=A4, leftMargin=22 * mm,
                    rightMargin=22 * mm, topMargin=29 * mm, bottomMargin=26 * mm,
                    title="Dobot 原子技能执行网关 - 接口与联调手册",
                    author="", subject="DBT-GW-INT-001 V1.1", pageCompression=1)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="manual", frames=frame, onPage=on_page))
    doc.multiBuild(story)
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cjk-font", type=Path, required=True)
    parser.add_argument("--mono-font", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output, args.cjk_font, args.mono_font)


if __name__ == "__main__":
    main()
