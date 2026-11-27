import { buttonCopy, tableCopy, workspaceCopy } from '../src/copy/common.ts';
import { evidenceCopy, resultCopy, routingCopy, unitCopy } from '../src/features/extract/copy.ts';
import { openWithDocx, resultRegion } from './support/app.ts';
import { DOCX_NAME, docxText } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';
import { ORIGIN } from './support/server.ts';

test('the result views switch, and 마크다운으로 보기 shows the raw markdown', async ({ page }) => {
  await openWithDocx(page);
  const result = resultRegion(page);
  const views = result.getByRole('tablist', { name: resultCopy.viewsLabel });
  const shownPanel = result.getByRole('tabpanel');

  await expect(views.getByRole('tab', { name: resultCopy.body })).toHaveAttribute('aria-selected', 'true');
  await expect(shownPanel).toHaveCount(1);

  await views.getByRole('tab', { name: resultCopy.tables(1) }).click();
  await expect(shownPanel).toHaveAccessibleName(resultCopy.tables(1));
  await expect(shownPanel.getByText(resultCopy.tableMeta(unitCopy.page.where(1), 2, 3))).toBeVisible();
  await expect(shownPanel.getByRole('table')).toHaveCount(1);
  await expect(shownPanel.getByText(docxText.paragraph)).toHaveCount(0);

  await views.getByRole('tab', { name: resultCopy.visuals(0) }).click();
  await expect(shownPanel).toHaveAccessibleName(resultCopy.visuals(0));
  await expect(shownPanel.getByText(resultCopy.noVisuals)).toBeVisible();

  await views.getByRole('tab', { name: resultCopy.chunks }).click();
  await expect(shownPanel).toHaveAccessibleName(resultCopy.chunks);
  await expect(shownPanel.getByRole('listitem').first()).toContainText(docxText.paragraph);

  await views.getByRole('tab', { name: resultCopy.markdown }).click();
  await expect(shownPanel).toHaveAccessibleName(resultCopy.markdown);
  const raw = shownPanel.locator('pre');
  // Markdown syntax shows as text, not rendered: no heading and no table.
  await expect(raw).toContainText(`# ${docxText.heading}`);
  await expect(raw).toContainText(`| ${docxText.table[0].join(' | ')} |`);
  await expect(raw).toContainText('| --- | --- | --- |');
  await expect(shownPanel.getByRole('table')).toHaveCount(0);
  await expect(shownPanel.getByRole('heading')).toHaveCount(0);

  await views.getByRole('tab', { name: resultCopy.body }).click();
  await expect(shownPanel).toHaveAccessibleName(resultCopy.body);
  await expect(shownPanel.getByText(docxText.paragraph)).toBeVisible();
});

test('the evidence column says how the page was read, and 자세히 shows the raw record', async ({ page }) => {
  await openWithDocx(page);
  const evidence = page.getByRole('region', { name: workspaceCopy.evidence, exact: true });
  await expect(evidence.getByRole('heading', { name: evidenceCopy.title })).toBeVisible();

  // The server read page 1 as table_heavy / ruled_table from the DOCX structure.
  const sentence = routingCopy.sentence(
    unitCopy.page.where(1),
    routingCopy.phrase(routingCopy.why.tableHeavy(unitCopy.page.noun), routingCopy.how.nativeTable[0]),
  );
  await expect(evidence.getByRole('listitem')).toHaveText([sentence]);

  // The raw record stays folded: its technical names are not on screen yet.
  const toggle = evidence.getByRole('button', { name: evidenceCopy.report });
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  const report = evidence.locator('pre');
  await expect(report).toBeHidden();
  expect(await evidence.innerText()).not.toContain('table_heavy');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(report).toBeVisible();
  await expect(report).toContainText(`\`${DOCX_NAME}\``);
  await expect(report).toContainText('`table_heavy`');
});

test('[엑셀용으로 복사하기] puts TSV and an HTML table on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: ORIGIN });
  await openWithDocx(page);
  const body = resultRegion(page).getByRole('tabpanel', { name: resultCopy.body });

  // The empty result line is already in the accessibility tree, as a live
  // region must be for screen readers to read a new message.
  const status = body.getByRole('figure').filter({ hasText: resultCopy.tableName(1) }).getByRole('status');
  await expect(status).toHaveCount(1);
  await expect(status).toBeEmpty();

  // Start from a known clipboard, so a copy that silently did nothing fails.
  await page.evaluate(() => navigator.clipboard.writeText('-'));
  await body.getByRole('button', { name: buttonCopy.copyForExcel }).click();

  const message = tableCopy.copied(resultCopy.tableName(1));
  await expect(body.getByRole('status').filter({ hasText: message })).toBeVisible();

  const clip = await page.evaluate(async () => {
    const [item] = await navigator.clipboard.read();
    if (!item) return null;
    const read = async (type: string) => (item.types.includes(type) ? (await item.getType(type)).text() : null);
    return { types: [...item.types], plain: await read('text/plain'), html: await read('text/html') };
  });
  expect(clip).not.toBeNull();
  expect(clip?.types).toEqual(expect.arrayContaining(['text/plain', 'text/html']));

  const lines = (clip?.plain ?? '').split(/\r?\n/);
  expect(lines).toEqual(docxText.table.map((row) => row.join('\t')));

  const html = clip?.html ?? '';
  expect(html).toMatch(/<table[\s>]/);
  for (const cell of docxText.table[0]) expect(html).toContain(`<th>${cell}</th>`);
  for (const cell of docxText.table[1]) expect(html).toContain(`<td>${cell}</td>`);

  // A second copy empties the line before the same message comes back, so
  // screen readers read it again. An unchanged line would not be read.
  await status.evaluate((line) => {
    const seen = { emptied: false };
    (window as unknown as { copyLine: typeof seen }).copyLine = seen;
    new MutationObserver((records) => {
      if (records.some((record) => record.removedNodes.length > 0)) seen.emptied = true;
    }).observe(line, { childList: true });
  });
  await body.getByRole('button', { name: buttonCopy.copyForExcel }).click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { copyLine: { emptied: boolean } }).copyLine.emptied))
    .toBe(true);
  await expect(status).toContainText(message);
});
