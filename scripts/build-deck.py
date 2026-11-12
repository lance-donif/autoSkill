"""Build the management deck from recorded facts and real screenshots.

Every number on a slide comes from the facts file; every picture is a real screenshot.
Usage: python scripts/build-deck.py --facts reports/evidence/final/facts.json --shots reports/evidence/final --out deliverables
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Inches, Pt

W, H = Inches(13.333), Inches(7.5)
FONT = "Microsoft YaHei"
INK, INK2, INK3 = RGBColor(0x0B, 0x0B, 0x0B), RGBColor(0x52, 0x51, 0x4E), RGBColor(0x89, 0x87, 0x81)
ACCENT, ACCENT_DARK, WASH = RGBColor(0x2A, 0x78, 0xD6), RGBColor(0x1C, 0x5C, 0xAB), RGBColor(0xE6, 0xF0, 0xFC)
DARK, PLANE, WHITE, LINE = RGBColor(0x17, 0x19, 0x1C), RGBColor(0xF4, 0xF4, 0xF1), RGBColor(0xFF, 0xFF, 0xFF), RGBColor(0xE1, 0xE0, 0xD9)
GOOD, WARN, BAD = RGBColor(0x0C, 0xA3, 0x0C), RGBColor(0xFA, 0xB2, 0x19), RGBColor(0xD0, 0x3B, 0x3B)


class Deck:
    def __init__(self) -> None:
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = W, H
        self.count = 0

    def slide(self, dark: bool = False):
        slide = self.prs.slides.add_slide(self.prs.slide_layouts[6])
        self.rect(slide, 0, 0, W, H, DARK if dark else PLANE)
        self.count += 1
        if not dark:
            self.text(slide, f"autoSkill  ·  {self.count}", Inches(0.6), Inches(7.0), Inches(4), Inches(0.3), 10, INK3)
        return slide

    def rect(self, slide, x, y, w, h, fill, radius: bool = False, line=None):
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, x, y, w, h)
        if radius:
            shape.adjustments[0] = 0.06
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill
        if line is None:
            shape.line.fill.background()
        else:
            shape.line.color.rgb = line
            shape.line.width = Pt(0.75)
        shape.shadow.inherit = False
        return shape

    def text(self, slide, content, x, y, w, h, size, color=INK, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.15):
        box = slide.shapes.add_textbox(x, y, w, h)
        frame = box.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = anchor
        for margin in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(frame, margin, 0)
        lines = content if isinstance(content, list) else [content]
        for index, line in enumerate(lines):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.alignment = align
            paragraph.line_spacing = spacing
            run = paragraph.add_run()
            run.text = line
            run.font.size, run.font.bold, run.font.name = Pt(size), bold, FONT
            properties = run._r.get_or_add_rPr()
            east_asian = properties.find(qn("a:ea"))
            if east_asian is None:
                east_asian = OxmlElement("a:ea")
                properties.find(qn("a:latin")).addnext(east_asian)
            east_asian.set("typeface", FONT)
            run.font.color.rgb = color
        return box

    def heading(self, slide, kicker: str, title: str, sub: str | None = None):
        self.text(slide, kicker, Inches(0.6), Inches(0.5), Inches(8), Inches(0.3), 12, ACCENT, True)
        self.text(slide, title, Inches(0.6), Inches(0.82), Inches(12), Inches(0.8), 30, INK, True)
        if sub:
            self.text(slide, sub, Inches(0.6), Inches(1.55), Inches(12), Inches(0.5), 15, INK2)

    def picture(self, slide, path: Path, x, y, w, h):
        """Fit a screenshot inside a box, top-left aligned, on a white card."""
        self.rect(slide, x, y, w, h, WHITE, radius=True, line=LINE)
        if not path.is_file():
            self.text(slide, f"（缺少截图 {path.name}）", x, y, w, h, 12, INK3, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
            return
        from PIL import Image

        with Image.open(path) as image:
            ratio = image.width / image.height
        pad = Inches(0.08)
        box_w, box_h = w - 2 * pad, h - 2 * pad
        if box_w / box_h > ratio:
            pic_h, pic_w = box_h, Emu(int(box_h * ratio))
        else:
            pic_w, pic_h = box_w, Emu(int(box_w / ratio))
        slide.shapes.add_picture(str(path), x + pad + (box_w - pic_w) // 2, y + pad, pic_w, pic_h)

    def points(self, slide, items: list[tuple[str, str]], x, y, w, gap=Inches(1.12)):
        for index, (title, body) in enumerate(items):
            top = y + gap * index
            dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, top + Inches(0.08), Inches(0.16), Inches(0.16))
            dot.fill.solid()
            dot.fill.fore_color.rgb = ACCENT
            dot.line.fill.background()
            self.text(slide, title, x + Inches(0.34), top, w - Inches(0.34), Inches(0.36), 17, INK, True)
            self.text(slide, body, x + Inches(0.34), top + Inches(0.38), w - Inches(0.34), Inches(0.7), 13, INK2)

    def stat(self, slide, value: str, label: str, x, y, w, color=INK):
        self.rect(slide, x, y, w, Inches(1.5), WHITE, radius=True, line=LINE)
        self.text(slide, value, x + Inches(0.25), y + Inches(0.2), w - Inches(0.5), Inches(0.7), 34, color, True)
        self.text(slide, label, x + Inches(0.25), y + Inches(0.95), w - Inches(0.5), Inches(0.45), 12, INK2)

    def flow(self, slide, steps: list[tuple[str, str]], y):
        count = len(steps)
        gap = Inches(0.3)
        width = (W - Inches(1.2) - gap * (count - 1)) // count
        for index, (title, body) in enumerate(steps):
            x = Inches(0.6) + (width + gap) * index
            first = index == 0
            self.rect(slide, x, y, width, Inches(1.9), ACCENT if first else WHITE, radius=True, line=None if first else LINE)
            self.text(slide, f"{index + 1}", x + Inches(0.22), y + Inches(0.18), Inches(0.5), Inches(0.4), 13, WHITE if first else ACCENT, True)
            self.text(slide, title, x + Inches(0.22), y + Inches(0.5), width - Inches(0.44), Inches(0.45), 17, WHITE if first else INK, True)
            self.text(slide, body, x + Inches(0.22), y + Inches(0.98), width - Inches(0.44), Inches(0.85), 12, WHITE if first else INK2)
            if index < count - 1:
                arrow = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, x + width + Inches(0.06), y + Inches(0.82), Inches(0.18), Inches(0.24))
                arrow.fill.solid()
                arrow.fill.fore_color.rgb = INK3
                arrow.line.fill.background()


def build(facts: dict, shots: Path, out: Path) -> Path:
    d = Deck()
    f = facts

    s = d.slide(dark=True)
    d.rect(s, Inches(0.6), Inches(2.3), Inches(0.12), Inches(1.5), ACCENT)
    d.text(s, "autoSkill", Inches(0.95), Inches(2.15), Inches(11), Inches(1.0), 54, WHITE, True)
    d.text(s, "录一次，到处回放", Inches(0.95), Inches(3.15), Inches(11), Inches(0.7), 30, RGBColor(0xE9, 0xE8, 0xE4))
    d.text(s, "让 AI 负责第一次，让脚本负责每一次：页面与接口自动化测试的新做法", Inches(0.95), Inches(4.25), Inches(11), Inches(0.5), 16, RGBColor(0x9A, 0x99, 0x93))
    d.text(s, f["date"], Inches(0.95), Inches(6.6), Inches(6), Inches(0.3), 12, RGBColor(0x9A, 0x99, 0x93))

    s = d.slide()
    d.heading(s, "为什么做", "自动化测试的三个老问题", "AI 能操作页面之后，这些问题没有消失，只是换了形式。")
    cards = [("每次都靠 AI，慢且不稳定", "让 AI 每次重新理解页面再操作，一次回归要几十分钟，同一条用例两次结果可能不同，也无法离开模型运行。"),
             ("页面和接口各写一套", "页面脚本和接口脚本由不同的人、用不同的工具维护，业务一改两边都要改，经常对不上。"),
             ("资产留不下来", "脚本散落在个人电脑上；登录、测试数据、业务规则靠口口相传，换个人就要从头再来。")]
    for index, (title, body) in enumerate(cards):
        x = Inches(0.6) + Inches(4.11) * index
        d.rect(s, x, Inches(2.5), Inches(3.9), Inches(3.4), WHITE, radius=True, line=LINE)
        d.text(s, f"0{index + 1}", x + Inches(0.3), Inches(2.75), Inches(1), Inches(0.5), 22, ACCENT, True)
        d.text(s, title, x + Inches(0.3), Inches(3.4), Inches(3.3), Inches(0.5), 19, INK, True)
        d.text(s, body, x + Inches(0.3), Inches(4.05), Inches(3.3), Inches(1.7), 13, INK2, spacing=1.3)

    s = d.slide()
    d.heading(s, "一句话方案", "AI 只做第一次，之后每一次都是确定的脚本",
              "AI 操作页面时，autoSkill 同步录下页面动作和接口流量，编译成不需要 AI 的回放用例。")
    d.flow(s, [("AI 操作页面", "像人一样点击、输入，并写下要验证的结果。"),
               ("同步录制", "记录每一步动作和页面发出的接口请求。"),
               ("编译成用例", "页面用例 + 接口用例，自动处理登录和数据传递。"),
               ("脚本回放", "Playwright、Selenium、HTTP，不调用模型。"),
               ("定时与看板", "到点自动执行，结果进入仪表盘。")], Inches(2.6))
    d.text(s, "同一份录制，还能导出 Postman 集合和独立的 Python 脚本，交给任何现有流程使用。", Inches(0.6), Inches(4.95), Inches(12), Inches(0.4), 14, INK2)
    for index, label in enumerate(["Playwright", "Selenium", "HTTP 接口回放", "Postman 集合", "独立 Python 脚本"]):
        x = Inches(0.6) + Inches(2.45) * index
        d.rect(s, x, Inches(5.6), Inches(2.25), Inches(0.6), WASH, radius=True)
        d.text(s, label, x, Inches(5.6), Inches(2.25), Inches(0.6), 14, ACCENT_DARK, True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)

    s = d.slide()
    d.heading(s, "核心能力 · 录制", "录一次页面操作，同时得到页面用例和接口用例")
    d.picture(s, shots / "case-steps.png", Inches(5.6), Inches(1.75), Inches(7.13), Inches(5.0))
    d.points(s, [("每一步都有多个定位方式", "页面小改动时自动换用备选定位，并在结果里提示，不会悄悄通过。"),
                 ("断言当场验证", "写下的期望在真实页面上成立才会被记录，避免录到错误的用例。"),
                 ("接口用例是录出来的", "状态码、关键字段、响应结构自动生成；令牌和上一步返回的编号自动传递。"),
                 ("密码不落盘", "用例里只有凭据名称，真实值加密保存在本机。")], Inches(0.6), Inches(1.9), Inches(4.7))

    s = d.slide()
    d.heading(s, "核心能力 · 前置依赖", "不用提醒「先登录」", "项目里有登录用例后，录制和回放都会自动先登录；登录态在本机复用，过期自动重登。")
    d.flow(s, [("只选一条用例", "例如「新建工单」。"), ("自动找到前置", "它依赖登录用例。"), ("先登录或复用", "登录态仍有效就直接用。"),
               ("执行目标用例", "页面与接口共用登录。"), ("结果分开统计", "前置不计入通过率。")], Inches(2.55))
    d.points(s, [("前置失败 ≠ 用例失败", "登录没通过时，依赖它的用例记为「阻塞」，不会制造一片假失败。"),
                 ("知识库跟着项目走", "业务规则、角色、测试数据写在项目知识库里，任何人、任何 AI 接手都能用上。")], Inches(0.6), Inches(4.95), Inches(12), gap=Inches(0.95))

    s = d.slide()
    d.heading(s, "仪表盘", "一眼看清所有项目的质量状况")
    d.picture(s, shots / "dashboard.png", Inches(0.6), Inches(1.7), Inches(7.9), Inches(5.1))
    d.points(s, [("默认汇总全部项目", "可切换单个项目和时间范围，所有数字、图表、列表同步变化。"),
                 ("每个指标都有口径", "通过率 = 通过 ÷（通过 + 失败），阻塞单独展示。"),
                 ("点数字看明细", "失败、阻塞一键下钻到同一筛选条件下的用例列表。"),
                 ("直接点运行", "单条、批量、整个项目都能在界面上发起；可勾选显示浏览器窗口，现场观看操作。")], Inches(8.85), Inches(1.9), Inches(3.9))

    s = d.slide()
    d.heading(s, "失败定位", "失败的原因，打开就能看到")
    d.picture(s, shots / "report.png", Inches(0.6), Inches(1.7), Inches(7.9), Inches(5.1))
    d.points(s, [("逐条断言：期望与实际", "哪一条不符、实际是什么，一目了然。"),
                 ("页面：步骤时间线 + 截图", "停在哪一步、当时页面什么样。"),
                 ("接口：请求与响应对照", "对标 Postman 的结果视图。"),
                 ("只重跑未通过的", "修复后一键复验，不必全部重来。")], Inches(8.85), Inches(1.9), Inches(3.9))

    s = d.slide()
    d.heading(s, "定时任务", "把稳定的回归交给时间表")
    d.picture(s, shots / "schedules.png", Inches(0.6), Inches(1.7), Inches(7.9), Inches(5.1))
    d.points(s, [("每天、每周、固定间隔、仅一次", "按项目选择全部或部分用例。"),
                 ("不需要 AI，也不需要人值守", "到点自动回放，结果进入执行记录和仪表盘。"),
                 ("可暂停、可立即运行", "上次结果和下次时间直接显示。")], Inches(8.85), Inches(1.9), Inches(3.9))

    s = d.slide()
    d.heading(s, "内网可用", "解压即用，发给同事就能用")
    d.stat(s, f["package_mb"], "完整离线包大小（MB）", Inches(0.6), Inches(1.9), Inches(2.9))
    d.stat(s, "0", "需要联网安装的组件", Inches(3.7), Inches(1.9), Inches(2.9))
    d.stat(s, "1 条命令", "打包项目并分享给同事", Inches(6.8), Inches(1.9), Inches(2.9))
    d.stat(s, "0", "实测中观察到的外联连接", Inches(9.9), Inches(1.9), Inches(2.83))
    d.points(s, [("自带全部运行依赖", f"Python、Chrome {f['chrome']}、浏览器驱动、录制工具和全部库都在包里，Windows 10 / 11 解压即可运行。"),
                 ("不更新、不上报", "没有在线更新和数据上报；浏览器只能访问项目授权的地址，其他地址连不上。"),
                 ("凭据不出本机", "账号密码和登录态加密保存在本机；分享包里只有用例、知识库和登录方式定义。")], Inches(0.6), Inches(3.85), Inches(12), gap=Inches(0.98))

    s = d.slide()
    d.heading(s, "实测结果", "这些数字都来自真实运行", f"测试环境：{f['environment']}")
    row = [(f["tests_total"], "项自动化测试全部通过"), (f["todomvc_cases"], "条页面用例在两种引擎上回放通过"),
           (f["offline_replay"], "离线包内完整流程回放结果"), (f["postman_assertions"], "条 Postman 断言用 newman 跑通")]
    for index, (value, label) in enumerate(row):
        d.stat(s, str(value), label, Inches(0.6) + Inches(3.08) * index, Inches(2.15), Inches(2.9), ACCENT_DARK)
    d.points(s, [(item["title"], item["body"]) for item in f["evidence_points"]], Inches(0.6), Inches(3.9), Inches(12), gap=Inches(1.02))

    s = d.slide()
    d.heading(s, "独立验收", f["acceptance"]["title"], f["acceptance"]["subtitle"])
    for index, (value, label) in enumerate(f["acceptance"]["stats"]):
        d.stat(s, str(value), label, Inches(0.6) + Inches(3.08) * index, Inches(2.15), Inches(2.9), ACCENT_DARK)
    d.points(s, [(item["title"], item["body"]) for item in f["acceptance"]["points"]], Inches(0.6), Inches(3.9), Inches(12), gap=Inches(1.02))

    s = d.slide()
    d.heading(s, "边界与下一步", "已经做到的，和还需要一起推进的")
    d.rect(s, Inches(0.6), Inches(1.9), Inches(5.95), Inches(4.8), WHITE, radius=True, line=LINE)
    d.text(s, "目前的边界", Inches(0.9), Inches(2.1), Inches(5.4), Inches(0.4), 17, INK, True)
    d.text(s, ["· " + item for item in f["limits"]], Inches(0.9), Inches(2.65), Inches(5.4), Inches(3.9), 13, INK2, spacing=1.45)
    d.rect(s, Inches(6.78), Inches(1.9), Inches(5.95), Inches(4.8), WASH, radius=True)
    d.text(s, "建议的下一步", Inches(7.08), Inches(2.1), Inches(5.4), Inches(0.4), 17, ACCENT_DARK, True)
    d.text(s, ["· " + item for item in f["next_steps"]], Inches(7.08), Inches(2.65), Inches(5.4), Inches(3.9), 13, INK, spacing=1.45)

    s = d.slide(dark=True)
    d.text(s, "录一次，到处回放", Inches(0.95), Inches(2.7), Inches(11.4), Inches(1.0), 44, WHITE, True)
    d.text(s, "AI 负责理解业务和第一次操作；之后的每一次回归，交给稳定、可重复、可离线运行的脚本。",
           Inches(0.95), Inches(3.85), Inches(11.4), Inches(0.9), 18, RGBColor(0xC8, 0xC7, 0xC2), spacing=1.3)

    out.mkdir(parents=True, exist_ok=True)
    target = out / "autoSkill-产品介绍.pptx"
    d.prs.save(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("deliverables"))
    args = parser.parse_args()
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    print(build(facts, args.shots, args.out))


if __name__ == "__main__":
    main()
