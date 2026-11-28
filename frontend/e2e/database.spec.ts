import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import type { Locator, Page } from '@playwright/test';
import { appCopy, buttonCopy, errorCopy, tabLabels, workspaceCopy } from '../src/copy/common.ts';
import {
  exportCopy,
  piiDialogCopy,
  purposeCopy,
  resultCopy,
  settingsCopy,
  startCopy,
  validationCopy,
} from '../src/features/database/copy.ts';
import { openApp } from './support/app.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { expect, test } from './support/fixtures.ts';

function csvPayload(name: string, content: string) {
  return { name, mimeType: 'text/csv', buffer: Buffer.from(content, 'utf-8') };
}

const BRANCHES_CSV = csvPayload(
  'branches.csv',
  'branch_code,branch_name\nB01,강남점\nB02,서초점\n',
);

const CUSTOMERS_CSV = csvPayload(
  'customers.csv',
  'customer_id,customer_name,phone,email,grade\n' +
    '101,김민수,010-1234-5678,minsu@example.com,VIP\n' +
    '102,이서연,010-9876-5432,seoyeon@example.com,일반\n',
);

const ORDERS_CSV = csvPayload(
  'orders.csv',
  'order_id,customer_id,order_date,amount\n' +
    '1001,101,2026-11-01,12000\n' +
    '1002,102,2026-11-02,24000\n',
);

const LOGS_PATTERN_CSV = csvPayload(
  'logs.csv',
  'item_code,category,note\n' +
    'ORD-2026-SEOUL-001,A,x\n' +
    'ORD-2026-SEOUL-001,A,x\n' +
    'ORD-2026-BUSAN-002,B,y\n' +
    'ORD-2026-DAEGU-003,A,z\n' +
    'ORD-2026-INCHEON-004,B,w\n' +
    'ORD-2025-SEOUL-005,A,x\n' +
    'ORD-2025-BUSAN-006,B,y\n',
);

async function openDatabaseTab(page: Page): Promise<Locator> {
  await openApp(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  const panel = page.getByRole('tabpanel', { name: tabLabels.database, exact: true });
  await expect(panel.getByRole('heading', { level: 2, name: startCopy.title })).toBeVisible();
  return panel;
}

async function pickDatabaseFiles(panel: Locator, page: Page, files: ReturnType<typeof csvPayload>[]) {
  const chooser = page.waitForEvent('filechooser');
  await panel.getByRole('button', { name: buttonCopy.pickFiles }).click();
  await (await chooser).setFiles(files);
}

async function addMoreDatabaseFiles(panel: Locator, page: Page, files: ReturnType<typeof csvPayload>[]) {
  const chooser = page.waitForEvent('filechooser');
  await panel.getByRole('button', { name: buttonCopy.addFiles }).click();
  await (await chooser).setFiles(files);
}

async function expectNoSidewaysScroll(page: Page) {
  const { scrollWidth, viewportWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(viewportWidth);
}

test('auto-analyzes CSVs, re-validates inline edits, and masks PII via ConfirmDialog before saving SQLite', async ({
  page,
}, testInfo) => {
  const panel = await openDatabaseTab(page);
  await expectNoAxeViolations(page);

  await pickDatabaseFiles(panel, page, [BRANCHES_CSV, CUSTOMERS_CSV, ORDERS_CSV]);

  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByRole('button', { name: /변경 내용 반영/ })).toHaveCount(0);
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');

  const listRegion = panel.getByRole('region', { name: workspaceCopy.list, exact: true });
  await listRegion.getByRole('option', { name: /customers/i }).click();

  const evidenceRegion = panel.getByRole('region', { name: workspaceCopy.evidence, exact: true });
  await expect(evidenceRegion).toContainText('찾은 이유');
  await expect(evidenceRegion).toContainText('김*수');
  await expect(evidenceRegion).toContainText('010-****-5678');

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  const gradeInput = result.getByRole('textbox', { name: 'grade의 DB 열 이름' });
  await gradeInput.fill('2bad');
  await expect(result.getByText(validationCopy.invalidIdentifier)).toBeVisible();

  let revalCount = 0;
  page.on('request', (req) => {
    if (
      req.url().endsWith('/api/analyze-tables') &&
      req.method() === 'POST' &&
      (req.postData() ?? '').includes('session_id')
    ) {
      revalCount += 1;
    }
  });

  const revalReq = page.waitForRequest(
    (req) =>
      req.url().endsWith('/api/analyze-tables') &&
      req.method() === 'POST' &&
      (req.postData() ?? '').includes('session_id'),
  );
  await gradeInput.fill('customer_tier');
  await gradeInput.press('Enter');
  const req = await revalReq;
  expect(req.postData() ?? '').toContain('"target_column":"customer_tier"');

  // Focusing elsewhere (blur after Enter) must not trigger a duplicate revalidation.
  await gradeInput.blur();
  await expect(result.getByRole('textbox', { name: 'grade의 DB 열 이름' })).toHaveValue('customer_tier');
  expect(revalCount).toBe(1);
  await expectNoAxeViolations(page);

  // ConfirmDialog opens -> Esc cancels -> [가리고 저장하기] downloads masked SQLite.
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(piiDialogCopy.title(3));
  const maskSaveBtn = dialog.getByRole('button', { name: piiDialogCopy.primaryAction });
  await expect(maskSaveBtn).toBeFocused();
  await expectNoAxeViolations(page);

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');

  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('alertdialog').getByRole('button', { name: piiDialogCopy.primaryAction }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('autoetl_data.sqlite');

  const sqlitePath = testInfo.outputPath('downloaded.sqlite');
  await download.saveAs(sqlitePath);
  const bytes = await readFile(sqlitePath);
  expect(bytes.subarray(0, 15).toString('ascii')).toBe('SQLite format 3');

  const dumped = execFileSync(
    'python3',
    [
      '-c',
      'import json, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); rows=c.execute("SELECT customer_name, phone, customer_tier FROM customers ORDER BY customer_id").fetchall(); print(json.dumps(rows, ensure_ascii=False))',
      sqlitePath,
    ],
    { encoding: 'utf-8' },
  );
  expect(JSON.parse(dumped)).toEqual([
    ['김*수', '010-****-5678', 'VIP'],
    ['이*연', '010-****-5432', '일반'],
  ]);

  await expect(panel.locator('[aria-current="step"]')).toContainText('내보내기');
  await expect(result).toContainText('customer_name');
  await expect(result).toContainText('phone');
});

test('renders surrogate PK and decomposed string slot rows as read-only with default decomposeStrings=true', async ({
  page,
}) => {
  const panel = await openDatabaseTab(page);
  await pickDatabaseFiles(panel, page, [LOGS_PATTERN_CSV]);

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  await expect(result.getByText(resultCopy.surrogateSourceLabel)).toBeVisible({ timeout: 20_000 });

  // Surrogate key row shows [대표 키] badge and has no editable DB name textbox.
  const surrogateRow = result.locator('tr', { hasText: resultCopy.surrogateSourceLabel });
  await expect(surrogateRow).toContainText(resultCopy.badges.primaryKey);
  await expect(surrogateRow).toContainText(resultCopy.readOnlySurrogateNote);
  await expect(surrogateRow.getByRole('textbox')).toHaveCount(0);

  // Decomposed slot rows (e.g. item_code에서 나눈 열) are read-only and do not collide with item_code input.
  await expect(result.getByText(resultCopy.decomposedSourceLabel('item_code')).first()).toBeVisible();
  await expect(result.getByRole('textbox', { name: 'item_code의 DB 열 이름' })).toHaveCount(1);
});

test('keeps focus in column name input across debounced revalidation and disables settings while saving DB', async ({
  page,
}) => {
  const panel = await openDatabaseTab(page);
  await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  const gradeInput = result.getByRole('textbox', { name: 'grade의 DB 열 이름' });
  await expect(gradeInput).toBeVisible({ timeout: 20_000 });

  // Type slowly enough for the 400ms debounced revalidation to fire and return, then ensure focus stays in input.
  const firstRevalRes = page.waitForResponse(
    (res) =>
      res.url().endsWith('/api/analyze-tables') &&
      res.request().method() === 'POST' &&
      (res.request().postData() ?? '').includes('"target_column":"customer_t"'),
  );
  await gradeInput.click();
  await gradeInput.press('ControlOrMeta+a');
  await gradeInput.pressSequentially('customer_t');
  expect((await firstRevalRes).status()).toBe(200);
  await expect(gradeInput).toBeFocused();
  await expect(gradeInput).toHaveValue('customer_t');

  const secondRevalRes = page.waitForResponse(
    (res) =>
      res.url().endsWith('/api/analyze-tables') &&
      res.request().method() === 'POST' &&
      (res.request().postData() ?? '').includes('"target_column":"customer_tier"'),
  );
  await page.keyboard.type('ier');
  await page.keyboard.press('Enter');
  expect((await secondRevalRes).status()).toBe(200);
  await expect(gradeInput).toHaveValue('customer_tier');

  // Switch purpose to archive and expand 세부 설정 so settings controls are visible before saving.
  const archiveRevalRes = page.waitForResponse(
    (res) =>
      res.url().endsWith('/api/analyze-tables') &&
      res.request().method() === 'POST' &&
      (res.request().postData() ?? '').includes('"purpose":"archive"'),
  );
  await panel.getByText('원본 보관용', { exact: true }).click();
  expect((await archiveRevalRes).status()).toBe(200);
  await panel.getByRole('button', { name: settingsCopy.heading }).click();

  const engineSelect = panel.getByLabel(settingsCopy.dbEngine.label);
  const notesInput = panel.getByLabel(settingsCopy.notes.label);
  const archiveRadio = panel.getByRole('radio', { name: /원본 보관용/ });

  let releaseLoad!: () => void;
  const loadHeld = new Promise<void>((resolve) => {
    releaseLoad = resolve;
  });
  let loadIntercepted = false;
  await page.route('**/api/etl/load-db', async (route) => {
    loadIntercepted = true;
    await loadHeld;
    await route.continue();
  });

  const downloadPromise = page.waitForEvent('download');
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  await expect.poll(() => loadIntercepted).toBe(true);

  await expect(panel.getByRole('button', { name: exportCopy.savingDb })).toBeDisabled();
  await expect(archiveRadio).toBeDisabled();
  await expect(engineSelect).toBeDisabled();
  await expect(notesInput).toBeDisabled();

  releaseLoad();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('autoetl_data.sqlite');
  await expect(result.getByText(resultCopy.saved.heading)).toBeVisible({ timeout: 20_000 });
  await expect(engineSelect).toBeEnabled();
  await expect(notesInput).toBeEnabled();
  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeEnabled();
});

test('blocks saving while replacement analysis fails or is in progress, and supports save-as-is', async ({
  page,
}, testInfo) => {
  const panel = await openDatabaseTab(page);
  await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);
  const saveBtn = panel.getByRole('button', { name: exportCopy.saveDb });
  await expect(saveBtn).toBeEnabled({ timeout: 20_000 });

  // Fail the next analyze call when adding a second file -> wait for error alert, verify save stays disabled.
  await page.route('**/api/analyze-tables', (route) => route.abort('failed'));
  await addMoreDatabaseFiles(panel, page, [ORDERS_CSV]);
  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  await expect(result.getByRole('alert')).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByText(exportCopy.staleNotice)).toBeVisible();
  await expect(saveBtn).toBeDisabled();

  // Recover by clicking [표 다시 살펴보기] after unrouting -> save becomes enabled again.
  await page.unroute('**/api/analyze-tables');
  await panel.getByRole('button', { name: exportCopy.reanalyze }).click();
  await expect(saveBtn).toBeEnabled({ timeout: 20_000 });

  // Confirm [그대로 저장하기] leaves values unmasked in SQLite.
  await saveBtn.click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('alertdialog').getByRole('button', { name: piiDialogCopy.secondaryAction }).click();
  const download = await downloadPromise;
  const sqlitePath = testInfo.outputPath('unmasked.sqlite');
  await download.saveAs(sqlitePath);
  const dumped = execFileSync(
    'python3',
    [
      '-c',
      'import json, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps(c.execute("SELECT customer_name FROM customers ORDER BY customer_id").fetchall(), ensure_ascii=False))',
      sqlitePath,
    ],
    { encoding: 'utf-8' },
  );
  expect(JSON.parse(dumped)).toEqual([['김민수'], ['이서연']]);
});

test('shows reject_all failure warnings and recovers expired sessions transparently on save', async ({
  page,
}) => {
  const panel = await openDatabaseTab(page);
  // Duplicate order_id in customers prevents reverse FK; 19 matching rows + 1 orphan row triggers reject_all failure.
  const customers = csvPayload(
    'customers.csv',
    'cust_no,order_id\n' +
      '1,1001\n2,1001\n3,1002\n4,1002\n5,1003\n' +
      '6,1003\n7,1004\n8,1004\n9,1005\n10,1005\n' +
      '11,1001\n12,1002\n13,1003\n14,1004\n15,1005\n' +
      '16,1001\n17,1002\n18,1003\n19,1004\n20,9999\n',
  );
  const orders = csvPayload(
    'orders.csv',
    'order_id,amount\n1001,100\n1002,200\n1003,300\n1004,400\n1005,500\n',
  );
  await pickDatabaseFiles(panel, page, [customers, orders]);
  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeEnabled({ timeout: 20_000 });

  // Switch orphan policy to reject_all.
  await panel.getByRole('button', { name: settingsCopy.heading }).click();
  await panel.getByLabel(settingsCopy.orphanPolicy.label).selectOption('reject_all');

  // Force the next /api/etl/load-db to return 404 expired once to verify auto-recovery.
  let intercepted = false;
  await page.route('**/api/etl/load-db', async (route) => {
    if (!intercepted) {
      intercepted = true;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: '분석 세션을 찾을 수 없다: expired' }),
      });
      return;
    }
    await route.continue();
  });

  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  await expect(result.getByText(resultCopy.saved.failedTitle)).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText(resultCopy.saved.warningsHeading)).toBeVisible();
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');
});

test('archive purpose shows inline notice immediately and skips the PII confirmation dialog', async ({
  page,
}) => {
  const panel = await openDatabaseTab(page);

  await panel.getByText('원본 보관용', { exact: true }).click();
  await expect(panel.getByRole('radio', { name: /원본 보관용/ })).toBeChecked();
  await expect(panel.getByText(purposeCopy.archiveNotice)).toBeVisible();

  await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);
  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByText(purposeCopy.archiveNotice)).toBeVisible();

  const downloadPromise = page.waitForEvent('download');
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('autoetl_data.sqlite');
});

test('rejects legacy .xls locally with actionable guidance and keeps axe clean on error', async ({
  page,
  pageLog,
}) => {
  const panel = await openDatabaseTab(page);
  const beforeRequests = pageLog.requests.length;

  await pickDatabaseFiles(panel, page, [
    { name: 'legacy_data.xls', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('xls') },
  ]);

  const alert = panel.getByRole('region', { name: workspaceCopy.result, exact: true }).getByRole('alert');
  await expect(alert).toContainText(errorCopy.unsupportedFile.what);
  await expect(alert).toContainText(errorCopy.unsupportedFile.xlsWhy);
  expect(
    pageLog.requests.slice(beforeRequests).filter((u) => u.includes('/api/analyze-tables')),
  ).toEqual([]);
  await expectNoAxeViolations(page);
});

test.describe('database tab at 320px wide', () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test('has no sideways scroll or axe violations on start and result screens', async ({ page }) => {
    const panel = await openDatabaseTab(page);
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);

    await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);
    await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);
  });
});
