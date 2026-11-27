/** Steps several specs share: open the screen, add a file, read through the API. */
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import type { APIRequestContext, Locator, Page, TestInfo } from '@playwright/test';
import { buttonCopy, workspaceCopy } from '../../src/copy/common.ts';
import { startCopy, statusText } from '../../src/features/extract/copy.ts';
import { DOCX_NAME, docxFile, docxText } from './docx.ts';
import { expect } from './fixtures.ts';
import { APP_PATH, ORIGIN } from './server.ts';

export interface FilePayload {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

/** Opens /next/ and waits for the extract tab's start screen. */
export async function openApp(page: Page): Promise<void> {
  await page.goto(APP_PATH);
  await expect(page.getByRole('heading', { level: 2, name: startCopy.title })).toBeVisible();
}

/** The tab's own status line (the bottom line, not a table's copy message). */
export function statusLine(page: Page, text: string): Locator {
  return page.getByRole('status').filter({ hasText: text });
}

/** The 결과 column of the workspace. */
export function resultRegion(page: Page): Locator {
  return page.getByRole('region', { name: workspaceCopy.result, exact: true });
}

/** Picks a file with [파일 고르기] through the browser's file chooser. */
export async function pickFile(page: Page, file: FilePayload): Promise<void> {
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: buttonCopy.pickFiles }).click();
  await (await chooser).setFiles(file);
}

/** Waits until the tab says it has read the fixture DOCX. */
export async function expectDocxRead(page: Page, name: string = DOCX_NAME): Promise<void> {
  await expect(statusLine(page, statusText.doneOne(name, 1, 0))).toBeVisible();
}

/** Opens the screen and reads the fixture DOCX, ending on the result screen. */
export async function openWithDocx(page: Page): Promise<void> {
  await openApp(page);
  await pickFile(page, docxFile());
  await expectDocxRead(page);
  await expect(resultRegion(page).getByText(docxText.paragraph)).toBeVisible();
}

/**
 * Drops files on the element with real browser drag events (CDP
 * `Input.dispatchDragEvent`), the way a file dragged from the desktop
 * arrives. CDP drags take file paths, so each file is written to its own
 * folder under the test's output folder and keeps its name. Use ASCII
 * names: in a non-UTF-8 locale Chromium drops a non-ASCII path silently.
 */
export async function dropFiles(page: Page, target: Locator, files: readonly FilePayload[], testInfo: TestInfo) {
  const paths: string[] = [];
  for (const [index, file] of files.entries()) {
    const folder = testInfo.outputPath(`drop-${index}`);
    await mkdir(folder, { recursive: true });
    const path = join(folder, file.name);
    await writeFile(path, file.buffer);
    paths.push(path);
  }
  const box = await target.boundingBox();
  if (!box) throw new Error('drop target is not visible');
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const data = { items: [], files: paths, dragOperationsMask: 1 };

  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send('Input.dispatchDragEvent', { type: 'dragEnter', x, y, data });
    await cdp.send('Input.dispatchDragEvent', { type: 'dragOver', x, y, data });
    await cdp.send('Input.dispatchDragEvent', { type: 'drop', x, y, data });
  } finally {
    await cdp.detach();
  }
}

/** Reads a file through POST /api/parse directly, as another client would. Returns the bundle id. */
export async function parseThroughApi(request: APIRequestContext, file: FilePayload): Promise<string> {
  const response = await request.post('/api/parse', {
    headers: { 'Content-Type': 'application/json', Origin: ORIGIN },
    data: JSON.stringify({ filename: file.name, content_base64: file.buffer.toString('base64'), options: {} }),
  });
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { bundle_id?: unknown };
  expect(typeof body.bundle_id).toBe('string');
  return String(body.bundle_id);
}
