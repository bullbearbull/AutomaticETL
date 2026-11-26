import assert from 'node:assert/strict';
import { test } from 'node:test';
import { copyTable, escapeHtml, rowsToHtml, rowsToTsv } from '../../src/lib/tableClipboard.ts';

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
