import { dropZoneCopy, errorCopy, errorTemplateCopy, workspaceCopy } from '../src/copy/common.ts';
import { listCopy, resultCopy } from '../src/features/extract/copy.ts';
import {
  dropFiles,
  expectDocxRead,
  openApp,
  pickFile,
  resultRegion,
} from './support/app.ts';
import { DOCX_NAME, DROP_DOCX_NAME, docxFile, docxText, XLS_NAME, xlsFile } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';

test('a DOCX picked with [파일 고르기] shows its paragraph and table', async ({ page, pageLog }) => {
  await openApp(page);
  await pickFile(page, docxFile());

  await expectDocxRead(page);
  expect(pageLog.requests.filter((url) => url.endsWith('/api/parse'))).toHaveLength(1);

  const result = resultRegion(page);
  await expect(result.getByRole('heading', { level: 2, name: DOCX_NAME })).toBeVisible();
  const body = result.getByRole('tabpanel', { name: resultCopy.body });
  await expect(body.getByText(docxText.paragraph)).toBeVisible();

  const table = body.getByRole('table');
  await expect(table).toHaveCount(1);
  await expect(table.getByRole('columnheader')).toHaveText([...docxText.table[0]]);
  await expect(table.getByRole('row').nth(1).getByRole('cell')).toHaveText([...docxText.table[1]]);
});

test('a DOCX dropped on the drop zone is read', async ({ page }, testInfo) => {
  await openApp(page);
  const zone = page.getByText(dropZoneCopy.prompt);
  await dropFiles(page, zone, [docxFile(DROP_DOCX_NAME)], testInfo);

  await expectDocxRead(page, DROP_DOCX_NAME);
  await expect(resultRegion(page).getByText(docxText.paragraph)).toBeVisible();
  await expect(resultRegion(page).getByRole('table')).toBeVisible();
});

test('an old .xls file gets an error notice and is never sent', async ({ page, pageLog }) => {
  await openApp(page);
  await pickFile(page, xlsFile());

  const alert = resultRegion(page).getByRole('alert');
  await expect(alert).toBeVisible();
  // 무슨 일 · 왜 · 어떻게
  await expect(alert.getByText(errorCopy.unsupportedFile.what)).toBeVisible();
  await expect(alert.getByRole('term')).toHaveText([errorTemplateCopy.why, errorTemplateCopy.how]);
  await expect(alert.getByRole('definition')).toHaveText([
    errorCopy.unsupportedFile.xlsWhy,
    errorCopy.unsupportedFile.xlsHow,
  ]);
  await expect(page.getByRole('region', { name: workspaceCopy.list, exact: true }).getByText(listCopy.unsupported)).toBeVisible();
  await expect(resultRegion(page).getByRole('heading', { name: XLS_NAME })).toBeVisible();

  expect(pageLog.requests.filter((url) => new URL(url).pathname === '/api/parse')).toEqual([]);
});
