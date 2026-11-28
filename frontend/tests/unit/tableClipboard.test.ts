import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { copyTable, escapeHtml, rowsToHtml, rowsToTsv, spreadsheetSafe } from '../../src/lib/tableClipboard.ts';

const rows = [
  ['구분', '담당 부서', '비고'],
  ['회의록', '총무팀', '전자 문서로 보관'],
  ['계약서', null, 3],
];

test('rowsToTsv joins cells with tabs and rows with CRLF', () => {
  assert.equal(rowsToTsv(rows), '구분\t담당 부서\t비고\r\n회의록\t총무팀\t전자 문서로 보관\r\n계약서\t\t3');
});

test('rowsToTsv quotes cells that hold tabs, line breaks or quotes', () => {
  assert.equal(rowsToTsv([['a\tb', '줄\n바꿈', '"인용"', '보통']]), '"a\tb"\t"줄\n바꿈"\t"""인용"""\t보통');
  assert.equal(rowsToTsv([['윈도우\r\n줄']]), '"윈도우\n줄"');
});

test('rowsToHtml escapes text and builds thead/tbody', () => {
  const html = rowsToHtml(
    [
      ['<b>', 'A&B'],
      ['"x"', "it's\n두 줄"],
    ],
    { headerRows: 1 },
  );
  assert.equal(
    html,
    '<table><thead><tr><th>&lt;b&gt;</th><th>A&amp;B</th></tr></thead>' +
      '<tbody><tr><td>&quot;x&quot;</td><td>it&#39;s<br>두 줄</td></tr></tbody></table>',
  );
  assert.equal(rowsToHtml([['a']]), '<table><tbody><tr><td>a</td></tr></tbody></table>');
  assert.equal(escapeHtml('<>&"\''), '&lt;&gt;&amp;&quot;&#39;');
});

class FakeItem {
  readonly items: Record<string, Blob>;
  constructor(items: Record<string, Blob>) {
    this.items = items;
  }
}

test('copyTable writes text/plain and text/html in one ClipboardItem', async () => {
  const written: unknown[] = [];
  const result = await copyTable(rows, {
    headerRows: 1,
    clipboard: { write: async (items) => void written.push(...items) },
    ClipboardItemImpl: FakeItem as unknown as new (items: Record<string, Blob>) => ClipboardItem,
  });
  assert.equal(result, 'rich');
  assert.equal(written.length, 1);
  const item = written[0] as FakeItem;
  assert.deepEqual(Object.keys(item.items).sort(), ['text/html', 'text/plain']);
  assert.equal(await item.items['text/plain']!.text(), rowsToTsv(rows));
  assert.ok((await item.items['text/html']!.text()).startsWith('<table><thead><tr><th>구분</th>'));
});

test('copyTable falls back to writeText when rich copy fails or is missing', async () => {
  let plain = '';
  const failing = await copyTable(rows, {
    clipboard: {
      write: async () => {
        throw new Error('denied');
      },
      writeText: async (text) => {
        plain = text;
      },
    },
    ClipboardItemImpl: FakeItem as unknown as new (items: Record<string, Blob>) => ClipboardItem,
  });
  assert.equal(failing, 'plain');
  assert.equal(plain, rowsToTsv(rows));

  plain = '';
  const missing = await copyTable(rows, {
    clipboard: { writeText: async (text) => void (plain = text) },
    ClipboardItemImpl: undefined,
  });
  assert.equal(missing, 'plain');
  assert.equal(plain, rowsToTsv(rows));
});

test('copyTable rejects when no clipboard method works', async () => {
  await assert.rejects(copyTable(rows, { clipboard: {}, ClipboardItemImpl: undefined }));
});

// The same cases the Python CSV writer is tested with.
const formulaCases = (
  JSON.parse(
    readFileSync(new URL('../../../tests/fixtures/spreadsheet_formula_cells.json', import.meta.url), 'utf8'),
  ) as { cases: { input: string; expected: string }[] }
).cases;

test('spreadsheetSafe follows the shared formula cell cases', () => {
  assert.ok(formulaCases.length > 0);
  for (const { input, expected } of formulaCases) {
    assert.equal(spreadsheetSafe(input), expected, JSON.stringify(input));
  }
});

test('rowsToTsv and rowsToHtml guard formula text cells but leave numbers alone', () => {
  const guarded = [
    ['=구분', '금액'],
    ['=1+1', '-1,234'],
    ['\t=1+1', -3],
  ];
  assert.equal(rowsToTsv(guarded), "'=구분\t금액\r\n'=1+1\t-1,234\r\n\"'\t=1+1\"\t-3");
  assert.equal(rowsToTsv([[-1e-7, '\r=1']]), "-1e-7\t\"'\n=1\"");
});

test('rowsToHtml marks formula text cells as Excel text instead of adding an apostrophe', () => {
  const text = ` style="mso-number-format:'\\@'"`;
  const guarded = [
    ['=구분', '금액'],
    ['=1+1', '-1,234'],
    ['\t=1+1', -3],
    ['@<b>', '-'],
    ['\r=1', '총무팀'],
  ];
  assert.equal(
    rowsToHtml(guarded, { headerRows: 1 }),
    `<table><thead><tr><th${text}>=구분</th><th>금액</th></tr></thead>` +
      `<tbody><tr><td${text}>=1+1</td><td>-1,234</td></tr>` +
      `<tr><td${text}>\t=1+1</td><td>-3</td></tr>` +
      `<tr><td${text}>@&lt;b&gt;</td><td>-</td></tr>` +
      `<tr><td${text}><br>=1</td><td>총무팀</td></tr></tbody></table>`,
  );
  assert.equal(rowsToHtml([[-1e-7, '+5%', "'=1+1"]]), '<table><tbody><tr><td>-1e-7</td><td>+5%</td><td>&#39;=1+1</td></tr></tbody></table>');
  for (const { input, expected } of formulaCases) {
    const guardedCell = expected !== input;
    assert.equal(
      rowsToHtml([[input]]).includes('mso-number-format'),
      guardedCell,
      JSON.stringify(input),
    );
  }
});
