/**
 * A synthetic DOCX built in memory: a heading, a paragraph and a table with
 * 2 rows and 3 columns. All text is made up. Only `word/document.xml` is
 * needed; the parser reads it without the other OOXML parts.
 */
import { storeZip } from './zip.ts';

export const DOCX_NAME = '분기 점검 예시.docx';
/** For drops: CDP drags need an ASCII path (see dropFiles). */
export const DROP_DOCX_NAME = 'drop-check.docx';
export const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export const docxText = {
  heading: '가상 사업 점검 요약',
  paragraph: '이 문서는 화면 테스트를 위해 지어낸 내용이에요.',
  table: [
    ['항목', '담당 부서', '상태'],
    ['예산 검토', '기획팀', '완료'],
  ],
} as const;

function escapeXml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function paragraph(text: string, heading = false): string {
  const props = heading ? '<w:pPr><w:pStyle w:val="Heading1"/><w:outlineLvl w:val="0"/></w:pPr>' : '';
  return `<w:p>${props}<w:r><w:t>${escapeXml(text)}</w:t></w:r></w:p>`;
}

function table(rows: readonly (readonly string[])[]): string {
  const width = rows[0]?.length ?? 0;
  const grid = `<w:tblGrid>${'<w:gridCol w:w="2400"/>'.repeat(width)}</w:tblGrid>`;
  const body = rows
    .map((row) => `<w:tr>${row.map((cell) => `<w:tc>${paragraph(cell)}</w:tc>`).join('')}</w:tr>`)
    .join('');
  return `<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>${grid}${body}</w:tbl>`;
}

export function makeDocx(): Buffer {
  const documentXml =
    '<?xml version="1.0" encoding="UTF-8"?>' +
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
    paragraph(docxText.heading, true) +
    paragraph(docxText.paragraph) +
    table(docxText.table) +
    '</w:body></w:document>';
  return storeZip([{ name: 'word/document.xml', data: documentXml }]);
}

/** The DOCX as `setInputFiles` takes it. */
export function docxFile(name: string = DOCX_NAME) {
  return { name, mimeType: DOCX_MIME, buffer: makeDocx() };
}

/** A few bytes named like an old Excel file. The tab refuses it before any request. */
export const XLS_NAME = '보고서.xls';

export function xlsFile() {
  return { name: XLS_NAME, mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('not a real workbook') };
}
