import { readFile } from 'node:fs/promises';
import type { Page, Response } from '@playwright/test';
import { exportCopy, rereadCopy } from '../src/features/extract/copy.ts';
import { openWithDocx, parseThroughApi, statusLine } from './support/app.ts';
import { docxFile } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';

/** How many ZIPs the server keeps (`_MAX_CACHED_BUNDLES` in server.py). */
const SERVER_KEEPS_ZIPS = 10;

function zipResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => new URL(response.url()).pathname.startsWith('/api/download/'));
}

/** The file name in a Content-Disposition header: `filename*` (RFC 5987) first, then `filename`. */
function dispositionName(header: string | undefined): string {
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header ?? '');
  if (extended?.[1]) return decodeURIComponent(extended[1].trim());
  const plain = /filename="([^"]*)"/i.exec(header ?? '');
  return plain?.[1] ?? '';
}

test('[ZIP으로 저장하기] saves the ZIP under the name the server gives', async ({ page }) => {
  await openWithDocx(page);

  const response = zipResponse(page);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: exportCopy.saveZip }).click();

  expect((await response).status()).toBe(200);
  const serverName = dispositionName((await response).headers()['content-disposition']);
  expect(serverName).toMatch(/\.zip$/);
  expect((await download).suggestedFilename()).toBe(serverName);

  const saved = await readFile(await (await download).path());
  expect(saved.subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  await expect(statusLine(page, exportCopy.saveStarted)).toBeVisible();
});

test('a ZIP the server has dropped offers [다시 읽기] and saves nothing', async ({ page, request }) => {
  await openWithDocx(page);

  // Other reads push the shown result's ZIP out of the server's cache.
  for (let index = 0; index < SERVER_KEEPS_ZIPS; index += 1) {
    await parseThroughApi(request, docxFile(`other-${index}.docx`));
  }

  let downloads = 0;
  page.on('download', () => {
    downloads += 1;
  });
  const response = zipResponse(page);
  await page.getByRole('button', { name: exportCopy.saveZip }).click();
  expect((await response).status()).toBe(404);

  await expect(page.getByText(exportCopy.expired)).toBeVisible();
  await expect(page.getByRole('button', { name: rereadCopy.button, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: exportCopy.saveZip })).toHaveCount(0);
  await expect(statusLine(page, exportCopy.saveStarted)).toHaveCount(0);
  expect(downloads).toBe(0);
});
