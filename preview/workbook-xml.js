// Minimal OOXML package for the browser. Layout values come from the native workbook template.
import { previewZip } from './preview-zip.js';

const office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const packageRels = 'http://schemas.openxmlformats.org/package/2006/relationships';
const spreadsheet = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
const prefix = 'application/vnd.openxmlformats-officedocument.spreadsheetml.';
const xml = (value) => String(value ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const letter = (index) => String.fromCharCode(65 + index);
export const displayLines = (value, width) => String(value ?? '').split(/\r\n|\r|\n/).reduce((total, line) => total + Math.max(1, Math.ceil(Array.from(line).length * 1.8 / width)), 0);

function styles(template) {
  const colors = ['BFBFBF', ...template.colors, ...['Pass','Fail','NotRun'].map((status) => template.fills[status]), '18334A', 'E5EFF2', 'FFFFFF', 'E2F3EE', 'FCEAE8'];
  const fills = ['none','gray125'].map((pattern) => '<fill><patternFill patternType="' + pattern + '"/></fill>')
    .concat(colors.map((color) => '<fill><patternFill patternType="solid"><fgColor rgb="FF' + color + '"/><bgColor indexed="64"/></patternFill></fill>'));
  const border = ['left','right','top','bottom'].map((side) => '<' + side + ' style="thin"><color rgb="FFD9D9D9"/></' + side + '>').join('');
  const formats = [[0, 0, 1], [1, 2, 1], ...template.colors.map((_, i) => [1, 3 + i, 1]), [0, 7, 1], [0, 8, 1], [0, 9, 1], [1, 0, 1]];
  const heading = (font, fill, format = 0) => '<xf numFmtId="' + format + '" fontId="' + font + '" fillId="' + fill + '" borderId="0" xfId="0" applyFont="1" applyFill="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1" indent="1"/></xf>';
  const extras = [heading(2,10), heading(1,11), heading(0,12), heading(3,10), heading(4,11), heading(5,13), heading(6,14), heading(4,11,10)];
  const formatsXml = formats.map(([font, fill, edge]) => '<xf numFmtId="0" fontId="' + font + '" fillId="' + fill + '" borderId="' + edge + '" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="' + (font ? 'center' : 'top') + '" wrapText="1"/></xf>').concat(extras).join('');
  const fonts = [[11,false,'172D3B'],[11,true,'172D3B'],[20,true,'FFFFFF'],[11,true,'FFFFFF'],[19,true,'18334A'],[19,true,'21675E'],[19,true,'AC3937']];
  return '<styleSheet xmlns="' + spreadsheet + '"><fonts count="' + fonts.length + '">' + fonts.map(([size,bold,color]) => '<font><name val="宋体"/>' + (bold ? '<b/>' : '') + '<color rgb="FF' + color + '"/><sz val="' + size + '"/></font>').join('') + '</fonts><fills count="' + fills.length + '">' + fills.join('') + '</fills><borders count="2"><border/><border>' + border + '<diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="' + (formats.length + extras.length) + '">' + formatsXml + '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>';
}

function commentFiles(comments) {
  const shape = '<v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202" path="m,l,21600r21600,l21600,xe"><v:stroke joinstyle="miter"/><v:path gradientshapeok="t" o:connecttype="rect"/></v:shapetype>';
  const boxes = comments.map(([row], index) => '<v:shape id="_x0000_s' + (1025 + index) + '" type="#_x0000_t202" style="position:absolute;margin-left:60pt;margin-top:2pt;width:200pt;height:70pt;visibility:hidden" fillcolor="#ffffe1" o:insetmode="auto"><v:fill color2="#ffffe1"/><v:shadow on="t" color="black" obscured="t"/><v:path o:connecttype="none"/><v:textbox style="mso-direction-alt:auto"><div style="text-align:left"/></v:textbox><x:ClientData ObjectType="Note"><x:MoveWithCells/><x:SizeWithCells/><x:AutoFill>False</x:AutoFill><x:Row>' + (row - 1) + '</x:Row><x:Column>0</x:Column></x:ClientData></v:shape>').join('');
  const relationships = '<Relationships xmlns="' + packageRels + '"><Relationship Id="comments" Type="' + office + '/comments" Target="../comments1.xml"/><Relationship Id="vml" Type="' + office + '/vmlDrawing" Target="../drawings/comments.vml"/></Relationships>';
  return [['xl/comments1.xml', '<comments xmlns="' + spreadsheet + '"><authors><author>autoSkill</author></authors><commentList>' + comments.map(([row, note]) => '<comment ref="A' + row + '" authorId="0"><text><t xml:space="preserve">' + xml(note) + '</t></text></comment>').join('') + '</commentList></comments>'],
    ['xl/worksheets/_rels/sheet2.xml.rels', relationships], ['xl/drawings/comments.vml', '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">' + shape + boxes + '</xml>']];
}

function rowXml(row, index, widths) {
  const height = row.height || (row.header || row.depth ? 28 : Math.min(409, Math.max(48, ...row.cells.map((cell, i) => displayLines(cell, widths[i]) * 16 + 8))));
  return '<row r="' + index + '" ht="' + height + '" customHeight="1">' + row.cells.map((cell, i) => {
    let style = row.styles?.[i] ?? row.style ?? (row.header ? 1 : 0);
    if (row.depth) style = i < 12 ? 1 + Math.min(row.depth, 4) : 9;
    if (row.status && i === 9) style = { Pass: 6, Fail: 7, NotRun: 8 }[row.status];
    const ref = letter(i) + index;
    if (cell == null || cell === '') return '<c r="' + ref + '" s="' + style + '"/>';
    if (typeof cell === 'number') return '<c r="' + ref + '" s="' + style + '" t="n"><v>' + cell + '</v></c>';
    return '<c r="' + ref + '" s="' + style + '" t="inlineStr"><is><t xml:space="preserve">' + xml(cell) + '</t></is></c>';
  }).join('') + '</row>';
}

function sheetXml(sheet) {
  const primary = sheet.name === '功能测试用例', overview = sheet.name === '概览';
  const merges = (sheet.merges || []).map((ref) => '<mergeCell ref="' + ref + '"/>');
  const body = sheet.rows.map((row, i) => {
    if (row.depth) merges.push('<mergeCell ref="A' + (i + 1) + ':L' + (i + 1) + '"/>');
    return rowXml(row, i + 1, sheet.widths);
  }).join('');
  const view = overview ? '<sheetView showGridLines="0" tabSelected="1" zoomScale="85" workbookViewId="0"/>' : primary ? '<sheetView zoomScale="80" workbookViewId="0"><pane xSplit="3" ySplit="1" topLeftCell="D2" activePane="bottomRight" state="frozen"/><selection pane="bottomRight" activeCell="D2" sqref="D2"/></sheetView>' : '<sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView>';
  const cols = sheet.widths.map((width, i) => '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + width + '" customWidth="1"/>').join('');
  return '<worksheet xmlns="' + spreadsheet + '" xmlns:r="' + office + '"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><dimension ref="A1:' + letter(sheet.widths.length - 1) + sheet.rows.length + '"/><sheetViews>' + view + '</sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>' + cols + '</cols><sheetData>' + body + '</sheetData>' + (merges.length ? '<mergeCells count="' + merges.length + '">' + merges.join('') + '</mergeCells>' : '') + '<pageMargins left="0.5" right="0.5" top="0.75" bottom="0.75" header="0.3" footer="0.3"/><pageSetup paperSize="8" orientation="landscape" fitToWidth="1" fitToHeight="0"/>' + (overview && sheet.chartModules ? '<drawing r:id="dashboard"/>' : '') + (primary ? '<legacyDrawing r:id="vml"/>' : '') + '</worksheet>';
}

function chartParts(parts, sheet) {
  if (!parts?.['xl/drawings/drawing1.xml'] || !parts?.['xl/charts/chart1.xml'] || !parts?.['xl/charts/chart2.xml']) throw new Error('概览图表模板不完整，请刷新页面');
  function series(match, type, column, startText, endText) {
    const start = Number(startText);
    const end = start === 25 && ['F','H','I','J'].includes(column) ? 24 + sheet.chartModules : Number(endText) || start;
    const address = "'概览'!$" + column + '$' + start + (start === end ? '' : ':$' + column + '$' + end);
    const values = sheet.rows.slice(start - 1, end).map((row) => row.cells[column.charCodeAt(0) - 65]);
    const data = values.map((v, i) => '<pt idx="' + i + '"><v>' + xml(v) + '</v></pt>').join('');
    return '<' + type + 'Ref><f>' + address + '</f><' + type + 'Cache>' + (type === 'num' ? '<formatCode>0</formatCode>' : '') +
      '<ptCount val="' + values.length + '"/>' + data + '</' + type + 'Cache></' + type + 'Ref>';
  }
  const result = Object.entries(parts).map(([path, value]) => {
    if (!path.startsWith('xl/charts/')) return [path, value];
    const reference = /<(str|num)Ref><f>[^!]+!\$?([A-Z])\$?(\d+)(?::\$?[A-Z]\$?(\d+))?<\/f>[\s\S]*?<\/\1Ref>/g;
    let chart = value.replace(reference, series);
    if (path.endsWith('chart2.xml') && sheet.rows.slice(24, 24 + sheet.chartModules).some((row) => row.cells[10] > 5)) chart = chart.replace(/<majorUnit val="1"\s*\/>/g, '');
    return [path, chart];
  });
  result.push(['xl/worksheets/_rels/sheet1.xml.rels', '<Relationships xmlns="' + packageRels + '"><Relationship Id="dashboard" Type="' + office + '/drawing" Target="../drawings/drawing1.xml"/></Relationships>']);
  return result;
}

export function workbookBytes(template, sheets) {
  const comments = sheets[1].rows.flatMap((row, i) => row.note ? [[i + 1, row.note]] : []);
  const contentType = (part, type) => '<Override PartName="/' + part + '" ContentType="' + prefix + type + '+xml"/>';
  const overview = sheets[0];
  const parts = overview.chartModules ? chartParts(template.overviewParts, overview) : [];
  const overrides = parts.filter(([path]) => path.startsWith('xl/charts/') || path === 'xl/drawings/drawing1.xml')
    .map(([path]) => '<Override PartName="/' + path + '" ContentType="application/vnd.openxmlformats-officedocument.drawingml.' + (path.startsWith('xl/charts/') ? 'chart' : 'drawing') + '+xml"/>').join('');
  const files = [['[Content_Types].xml' , '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="vml" ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>' + contentType('xl/workbook.xml','sheet.main') + contentType('xl/styles.xml','styles') + contentType('xl/comments1.xml','comments') + overrides + sheets.map((_, i) => contentType('xl/worksheets/sheet' + (i + 1) + '.xml','worksheet')).join('') + '</Types>'],
    ['_rels/.rels', '<Relationships xmlns="' + packageRels + '"><Relationship Id="book" Type="' + office + '/officeDocument" Target="xl/workbook.xml"/></Relationships>'],
    ['xl/workbook.xml', '<workbook xmlns="' + spreadsheet + '" xmlns:r="' + office + '"><bookViews><workbookView/></bookViews><sheets>' + sheets.map((sheet, i) => '<sheet name="' + xml(sheet.name) + '" sheetId="' + (i + 1) + '" r:id="s' + (i + 1) + '"/>').join('') + '</sheets><definedNames><definedName name="_xlnm.Print_Titles" localSheetId="1">\'功能测试用例\'!$1:$1</definedName></definedNames></workbook>'],
    ['xl/_rels/workbook.xml.rels', '<Relationships xmlns="' + packageRels + '"><Relationship Id="styles" Type="' + office + '/styles" Target="styles.xml"/>' + sheets.map((_, i) => '<Relationship Id="s' + (i + 1) + '" Type="' + office + '/worksheet" Target="worksheets/sheet' + (i + 1) + '.xml"/>').join('') + '</Relationships>'],
    ['xl/styles.xml', styles(template)], ...commentFiles(comments), ...parts, ...sheets.map((sheet, i) => ['xl/worksheets/sheet' + (i + 1) + '.xml', sheetXml(sheet)])];
  return previewZip(files);
}
