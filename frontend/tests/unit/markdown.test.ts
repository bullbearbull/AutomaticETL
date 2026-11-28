import assert from 'node:assert/strict';
import { test } from 'node:test';
import { markdownToTree, safeHref, type MdNode } from '../../src/lib/markdown.ts';

/** Flattens the tree into a compact string for assertions. */
function show(nodes: MdNode[]): string {
  return nodes
    .map((node) => {
      if (node.type === 'text') return JSON.stringify(node.value);
      const attrs = Object.entries(node.attrs)
        .map(([key, value]) => ` ${key}=${String(value)}`)
        .join('');
      return `<${node.tag}${attrs}>${show(node.children)}</${node.tag}>`;
    })
    .join('');
}

test('headings, paragraphs and inline marks become elements', () => {
  const tree = markdownToTree('# 제목\n\n본문 **굵게** *기울임* `코드` ~~줄~~');
  assert.equal(
    show(tree),
    '<h1>"제목"</h1><p>"본문 "<strong>"굵게"</strong>" "<em>"기울임"</em>" "<code>"코드"</code>" "<s>"줄"</s></p>',
  );
});

/** Every element tag in the tree. */
function tags(nodes: MdNode[]): string[] {
  return nodes.flatMap((node) => (node.type === 'text' ? [] : [node.tag, ...tags(node.children)]));
}

test('raw HTML stays text and never becomes an element', () => {
  const tree = markdownToTree('<script>alert(1)</script>\n\n문단 <img src=x onerror=alert(1)>');
  assert.deepEqual(tags(tree), ['p', 'p']);
  const text = show(tree);
  assert.ok(text.includes('"<script>alert(1)</script>"'), text);
  assert.ok(text.includes('<img src=x onerror=alert(1)>"'), text);
});

test('unsafe link schemes never reach an href', () => {
  const tree = markdownToTree(
    '[a](javascript:alert(1)) [b](https://example.com/x) [c](#part) [d](data:text/html,hi) ' +
      '[e](VBScript:x) [f](ftp://example.com/f) [g](//evil.example/g)',
  );
  const text = show(tree);
  assert.ok(text.includes('<a href=https://example.com/x>"b"</a>'), text);
  assert.ok(text.includes('<a href=#part>"c"</a>'), text);
  // markdown-it accepts ftp: and protocol-relative links; the allow-list drops them.
  assert.ok(text.includes('<span>"f"</span>'), text);
  assert.ok(text.includes('<span>"g"</span>'), text);
  const hrefs = [...text.matchAll(/href=(\S+?)>/g)].map((match) => match[1]);
  assert.deepEqual(hrefs, ['https://example.com/x', '#part']);
});

test('safeHref checks the scheme after removing control characters', () => {
  assert.equal(safeHref('java\tscript:alert(1)'), undefined);
  assert.equal(safeHref(' javascript:alert(1)'), undefined);
  assert.equal(safeHref('//evil.example/x'), undefined);
  assert.equal(safeHref('/\\evil.example'), undefined);
  assert.equal(safeHref('\\/evil.example/x'), undefined);
  assert.equal(safeHref('\\\\evil.example/x'), undefined);
  assert.equal(safeHref('mailto:someone@example.com'), 'mailto:someone@example.com');
  assert.equal(safeHref('HTTPS://example.com'), 'HTTPS://example.com');
  assert.equal(safeHref('tables/table_1.csv'), 'tables/table_1.csv');
  assert.equal(safeHref(''), undefined);
  assert.equal(safeHref(null), undefined);
});

test('images become alt text, so no external request is possible', () => {
  const tree = markdownToTree('![조직도](https://example.com/a.png) ![](b.png)');
  assert.equal(show(tree), '<p>"[그림: 조직도] [그림]"</p>');
});

test('tables keep their structure and alignment', () => {
  const tree = markdownToTree('| 구분 | 금액 |\n| --- | ---: |\n| 회의비 | 120,000 |');
  assert.equal(
    show(tree),
    '<table><thead><tr><th>"구분"</th><th align=right>"금액"</th></tr></thead>' +
      '<tbody><tr><td>"회의비"</td><td align=right>"120,000"</td></tr></tbody></table>',
  );
});

test('tight list items drop the hidden paragraph; ordered lists keep start', () => {
  assert.equal(show(markdownToTree('- 하나\n- 둘')), '<ul><li>"하나"</li><li>"둘"</li></ul>');
  assert.equal(show(markdownToTree('3. 셋\n4. 넷')), '<ol start=3><li>"셋"</li><li>"넷"</li></ol>');
});

test('an ordered list that starts at 1 has no start attribute', () => {
  assert.equal(show(markdownToTree('1. 하나\n2. 둘')), '<ol><li>"하나"</li><li>"둘"</li></ol>');
  assert.equal(show(markdownToTree('0. 영\n1. 하나')), '<ol start=0><li>"영"</li><li>"하나"</li></ol>');
});

test('code blocks keep their text as is', () => {
  const tree = markdownToTree('```\n<b>그대로</b>\n```');
  assert.equal(show(tree), '<pre><code>"<b>그대로</b>\\n"</code></pre>');
});

test('line breaks: soft break is a newline, hard break is <br>', () => {
  assert.equal(show(markdownToTree('가\n나')), '<p>"가\\n나"</p>');
  assert.equal(show(markdownToTree('가  \n나')), '<p>"가"<br></br>"나"</p>');
});
