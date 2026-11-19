"""Native Excel charts and their visible source tables for a frozen report scope."""
from __future__ import annotations

from collections import Counter

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.chart import BarChart, DoughnutChart, Reference
from openpyxl.chart.data_source import AxDataSource, NumData, NumVal, StrData, StrRef, StrVal
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.series import DataPoint
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, Paragraph, ParagraphProperties
from openpyxl.drawing.text import Font as DrawingFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

STATUSES = ("NotRun", "Pass", "Fail")
LABELS = ("未执行", "通过", "失败")
COLORS = ("95A6B5", "238678", "C8504D")
NAVY, INK, MUTED, PALE, WHITE = "18334A", "243F50", "516B7B", "EDF3F5", "FFFFFF"
FIRST = 25
CHART_MODULES = 8


def _excel_text(value):
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]


def _cell(sheet, row, column, value, *, end=None, background=None, foreground=INK, size=11, bold=False, center=False):
    if end and end != column:
        sheet.merge_cells(start_row=row, end_row=row, start_column=column, end_column=end)
    cell = sheet.cell(row, column, _excel_text(value) if isinstance(value, str) else value)
    if isinstance(value, str):
        cell.data_type = "s"
    cell.font = Font(name="宋体", size=size, bold=bold, color=foreground)
    cell.alignment = Alignment(horizontal="center" if center else "left", vertical="center", indent=0 if center else 1,
                               shrink_to_fit=True)
    if background:
        cell.fill = PatternFill("solid", fgColor=background)
    return cell


def _chart_text(size=10):
    face = DrawingFont(typeface="宋体")
    properties = CharacterProperties(latin=face, ea=face, lang="zh-CN", sz=size * 100, solidFill=INK)
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=properties), endParaRPr=properties)])


def _categories(ref, values):
    points = [StrVal(idx=i, v=label) for i, label in enumerate(values)]
    return AxDataSource(strRef=StrRef(f=str(ref), strCache=StrData(pt=points, ptCount=len(points))))


def _cache_values(series, values):
    points = [NumVal(idx=i, v=value) for i, value in enumerate(values)]
    series.val.numRef.numCache = NumData(formatCode="0", pt=points, ptCount=len(points))


def _charts(sheet, totals, modules):
    ring = DoughnutChart()
    ring.width, ring.height = 12.1, 7
    ring.holeSize, ring.firstSliceAng = 66, 270
    ring.add_data(Reference(sheet, min_col=2, min_row=FIRST, max_row=FIRST + 2))
    series = ring.series[0]
    series.cat = _categories(Reference(sheet, min_col=1, min_row=FIRST, max_row=FIRST + 2), LABELS)
    _cache_values(series, [totals[status] for status in STATUSES])
    for i, color in enumerate(COLORS):
        point = DataPoint(idx=i)
        point.graphicalProperties.solidFill = color
        point.graphicalProperties.line.solidFill = WHITE
        series.data_points.append(point)
    ring.legend.position = "b"
    ring.legend.txPr = _chart_text()
    ring.dLbls = DataLabelList(showVal=True, showLegendKey=False, showCatName=False, showSerName=False, showPercent=False,
                              numFmt="0;-0;;")
    ring.dLbls.txPr = _chart_text(11)
    sheet.add_chart(ring, "A11")

    bars = BarChart()
    bars.type, bars.grouping, bars.overlap, bars.gapWidth = "bar", "stacked", 100, 55
    bars.width, bars.height = 17.2, 7
    bars.legend.position = "b"
    bars.legend.txPr = _chart_text()
    bars.x_axis.txPr, bars.y_axis.txPr = _chart_text(10), _chart_text(9)
    bars.x_axis.scaling.orientation = "maxMin"
    bars.y_axis.crosses = "max"
    bars.y_axis.scaling.min = 0
    bars.y_axis.numFmt = "0"
    if max(sum(counts.values()) for _, counts in modules) <= 5:
        bars.y_axis.majorUnit = 1
    end = FIRST + len(modules) - 1
    bars.add_data(Reference(sheet, min_col=8, max_col=10, min_row=FIRST - 1, max_row=end), titles_from_data=True)
    names = [name for name, _ in modules]
    for series, status, label, color in zip(bars.series, STATUSES, LABELS, COLORS, strict=True):
        series.cat = _categories(Reference(sheet, min_col=6, min_row=FIRST, max_row=end), names)
        series.tx.strRef.strCache = StrData(ptCount=1, pt=[StrVal(idx=0, v=label)])
        _cache_values(series, [counts[status] for _, counts in modules])
        series.graphicalProperties.solidFill = color
        series.graphicalProperties.line.noFill = True
    sheet.add_chart(bars, "F11")


def _metrics(sheet, totals):
    judged = totals["Pass"] + totals["Fail"]
    rate = totals["Pass"] / judged if judged else "—"
    cards = [("用例总数", totals.total(), INK), ("通过", totals["Pass"], COLORS[1]),
             ("失败", totals["Fail"], COLORS[2]), ("通过率", rate, INK)]
    for i, (label, value, color) in enumerate(cards):
        column = 1 + i * 3
        _cell(sheet, 7, column, label, end=column + 2, background=PALE, foreground=MUTED, bold=True, center=True)
        cell = _cell(sheet, 8, column, value, end=column + 2, background=PALE, foreground=color, size=21, bold=True, center=True)
        if label == "通过率" and judged:
            cell.number_format = "0.0%"


def _tables(sheet, totals, modules):
    for col, end, label in [(1, 1, "执行结果"), (2, 2, "数量"), (6, 7, "业务模块"), (8, 8, "未执行"),
                            (9, 9, "通过"), (10, 10, "失败"), (11, 11, "合计")]:
        _cell(sheet, 24, col, label, end=end, background=NAVY, foreground=WHITE, bold=True, center=True)
    for i, (status, label) in enumerate(zip(STATUSES, LABELS, strict=True)):
        _cell(sheet, FIRST + i, 1, label, background=PALE, bold=True)
        _cell(sheet, FIRST + i, 2, totals[status], foreground=COLORS[i], bold=True, center=True)
    for i, (name, counts) in enumerate(modules, FIRST):
        fill = PALE if (i - FIRST) % 2 == 0 else WHITE
        _cell(sheet, i, 6, name, end=7, background=fill)
        for j, status in enumerate(STATUSES, 8):
            _cell(sheet, i, j, counts[status], background=fill, center=True)
        _cell(sheet, i, 11, counts.total(), background=fill, bold=True, center=True)


def _grouped(rows):
    totals = Counter()
    per_module = {}
    for row in rows:
        group = row["group"]
        status = group["status"]
        totals[status] += 1
        path = group["original_business_path"]
        module = group["definition"].get("module")
        if not module or module == "未分组":
            module = path[1] if len(path) > 1 and path[1] != "【待确认：功能项】" else "未分组"
        module = _excel_text(module) or "未分组"
        per_module.setdefault(module, Counter())[status] += 1
    modules = sorted(per_module.items(), key=lambda item: (-item[1].total(), item[0]))
    return totals, modules


def render(book, snapshot, rows):
    project = snapshot["project"]
    totals, modules = _grouped(rows)
    sheet = book.create_sheet("概览", 0)
    book.active = sheet
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 85
    sheet.sheet_properties.tabColor = COLORS[1]
    for col in range(1, 13):
        sheet.column_dimensions[get_column_letter(col)].width = 13
    for i in range(1, max(28, FIRST + len(modules))):
        sheet.row_dimensions[i].height = 19
    sheet.row_dimensions[1].height, sheet.row_dimensions[8].height = 36, 37
    sheet.row_dimensions[6].height, sheet.row_dimensions[9].height = 7, 9
    heading = "测试用例设计概览" if snapshot["kind"] == "design" else "测试报告概览"
    _cell(sheet, 1, 1, heading, end=12, background=NAVY, foreground=WHITE, size=21, bold=True)
    metadata = snapshot.get("metadata", {})
    timestamp = next((metadata[key] for key in ("finished_at", "updated_at", "generated_at", "started_at", "created_at") if metadata.get(key)), None)
    for i, (label, value) in enumerate([("项目", project.get("name")), ("测试范围", snapshot.get("title")),
                                        ("被测地址", project.get("base_url")), ("记录时间", timestamp)], 2):
        _cell(sheet, i, 1, label, end=3, foreground=MUTED, bold=True, background=PALE)
        _cell(sheet, i, 4, value or "—", end=12)
    _metrics(sheet, totals)
    _cell(sheet, 10, 1, "执行结果分布", end=5, background=NAVY, foreground=WHITE, bold=True)
    _cell(sheet, 10, 6, "业务模块结果" + ("（按用例数取前 8 个）" if len(modules) > CHART_MODULES else ""),
          end=12, background=NAVY, foreground=WHITE, bold=True)
    _cell(sheet, 23, 1, "通过率 = 通过 ÷（通过 + 失败）", end=5, foreground=MUTED)
    _tables(sheet, totals, modules)
    if rows:
        _charts(sheet, totals, modules[:CHART_MODULES])
    else:
        _cell(sheet, 14, 1, "暂无测试用例", end=12, foreground=MUTED, size=14, center=True)
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    sheet.print_area = f"A1:L{sheet.max_row}"


def verify(sheet, rows):
    """Read back source counts, axis labels, and data bindings of each native chart."""
    totals, modules = _grouped(rows)
    number = len(modules[:CHART_MODULES])
    counts = [totals[status] for status in STATUSES]
    source = [(sheet.cell(FIRST + i, 1).value, sheet.cell(FIRST + i, 2).value) for i in range(3)]
    if source != list(zip(LABELS, counts, strict=True)) or sheet["A8"].value != totals.total():
        raise ValueError("概览的用例计数与主表不一致")
    for i, (module, counter) in enumerate(modules, FIRST):
        actual = [sheet.cell(i, col).value for col in (6, 8, 9, 10, 11)]
        if actual != [module, *[counter[s] for s in STATUSES], counter.total()]:
            raise ValueError("概览的模块统计与主表不一致")
    if not number:
        if sheet._charts:
            raise ValueError("空报告含有无数据的图表")
        return
    if [type(chart) for chart in sheet._charts] != [DoughnutChart, BarChart]:
        raise ValueError("概览缺少原生结果分布图或模块统计图")
    ring, bars = sheet._charts
    if len(ring.series) != 1 or len(bars.series) != 3 or bars.grouping != "stacked" or bars.type != "bar":
        raise ValueError("概览图表系列不完整")
    refs = [(ring.series[0], 1, 2, 3)]
    refs.extend((series, 6, column, number) for column, series in enumerate(bars.series, 8))
    for series, label_col, value_col, count in refs:
        end = FIRST + count - 1
        label_ref = str(Reference(sheet, min_col=label_col, min_row=FIRST, max_row=end))
        value_ref = str(Reference(sheet, min_col=value_col, min_row=FIRST, max_row=end))
        labels = [sheet.cell(i, label_col).value for i in range(FIRST, end + 1)]
        values = [sheet.cell(i, value_col).value for i in range(FIRST, end + 1)]
        if (series.cat.strRef.f != label_ref or series.val.numRef.f != value_ref
                or [p.v for p in series.cat.strRef.strCache.pt] != labels
                or [p.v for p in series.val.numRef.numCache.pt] != values):
            raise ValueError("概览图表引用或缓存值与统计表不一致")
