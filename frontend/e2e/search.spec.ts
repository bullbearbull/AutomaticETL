import type { Locator, Page } from '@playwright/test';
import { appCopy, buttonCopy, errorCopy, tabLabels, workspaceCopy } from '../src/copy/common.ts';
import { evidenceCopy, resultCopy, searchErrorCopy, settingsCopy, startCopy } from '../src/features/search/copy.ts';
import { openApp } from './support/app.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { expect, test } from './support/fixtures.ts';
import { ORIGIN } from './support/server.ts';
import { storeZip } from './support/zip.ts';

function escapeXml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function paragraph(text: string, heading = false): string {
  const props = heading ? '<w:pPr><w:pStyle w:val="Heading1"/><w:outlineLvl w:val="0"/></w:pPr>' : '';
  return `<w:p>${props}<w:r><w:t>${escapeXml(text)}</w:t></w:r></w:p>`;
}

function docxTable(rows: readonly (readonly string[])[]): string {
  const width = rows[0]?.length ?? 0;
  const grid = `<w:tblGrid>${'<w:gridCol w:w="2400"/>'.repeat(width)}</w:tblGrid>`;
  const body = rows
    .map((row) => `<w:tr>${row.map((cell) => `<w:tc>${paragraph(cell)}</w:tc>`).join('')}</w:tr>`)
    .join('');
  return `<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>${grid}${body}</w:tbl>`;
}

function buildDocxPayload(
  name: string,
  sections: ReadonlyArray<{ heading: string; body: string; table?: readonly (readonly string[])[] }>,
) {
  const xmlParts = sections
    .map((sec) => paragraph(sec.heading, true) + paragraph(sec.body) + (sec.table ? docxTable(sec.table) : ''))
    .join('');
  const xml =
    '<?xml version="1.0" encoding="UTF-8"?>' +
    `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${xmlParts}</w:body></w:document>`;
  return {
    name,
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: storeZip([{ name: 'word/document.xml', data: xml }]),
  };
}

const EDUCATION_DOC = buildDocxPayload('인사_및_교육규정.docx', [
  {
    heading: '제1장 총칙',
    body: '이 규정은 사내 직무 교육과 수료 기준에 관한 일반 원칙을 정한다.',
  },
  {
    heading: '제4조 교육 이수 기록의 보관 기간',
    body: '직원 교육 이수 기록과 수료증 사본의 보관 기간은 작성일로부터 5년으로 정한다. 보관 기간 동안 인사팀이 원본을 안전하게 관리한다.',
  },
]);

const SECURITY_DOC = buildDocxPayload('보안_및_자료관리규정.docx', [
  {
    heading: '제9조 접속 기록의 보관 기간',
    body: '시스템 접속 기록과 접근 이력의 보관 기간은 최소 3년으로 정한다. 보관 기간이 지난 로그는 분기마다 점검해 안전하게 파기한다.',
    table: [
      ['항목', '보유 연한', '책임 부서'],
      ['접속 기록', '3년', '정보보호팀'],
      ['승인 이력', '5년', '감사팀'],
    ],
  },
]);

const CLUB_DOC = buildDocxPayload('복리후생_및_동호회안내.docx', [
  {
    heading: '제5조 사내 동호회 활동비 지원',
    body: '사내 동호회 운영비와 영수증 증빙은 참고용으로 잠깐 보관한다. 연간 예산 신청서와 활동 결과표는 매년 12월에 제출한다.',
  },
]);

async function openSearchTab(page: Page): Promise<Locator> {
  await openApp(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  await tabList.getByRole('tab', { name: tabLabels.search }).click();
  const panel = page.getByRole('tabpanel', { name: tabLabels.search, exact: true });
  await expect(panel.getByRole('heading', { level: 2, name: startCopy.title })).toBeVisible();
  return panel;
}

async function pickSearchFiles(
  panel: Locator,
  page: Page,
  files: ReturnType<typeof buildDocxPayload>[],
) {
  const chooser = page.waitForEvent('filechooser');
  await panel.getByRole('button', { name: buttonCopy.pickFiles }).click();
  await (await chooser).setFiles(files);
}

async function addMoreSearchFiles(
  panel: Locator,
  page: Page,
  files: ReturnType<typeof buildDocxPayload>[],
) {
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

test('auto-prepares multiple docs, highlights matches cleanly, folds low relevance, and copies citation to Step 3', async ({
  page,
  context,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: ORIGIN });
  const panel = await openSearchTab(page);
  await expectNoAxeViolations(page);

  await pickSearchFiles(panel, page, [EDUCATION_DOC, SECURITY_DOC, CLUB_DOC]);

  const searchInput = panel.getByRole('searchbox', { name: resultCopy.searchForm.label });
  await expect(searchInput).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByRole('button', { name: /검색 준비하기/ })).toHaveCount(0);
  await expect(panel.locator('[aria-current="step"]')).toContainText('넣기');

  // Search "보관 기간" -> Step 2, exact regulation hits are primary, weak "보관" hit is folded.
  await searchInput.fill('보관 기간');
  await panel.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');
  await expect(result.getByText('제4조 교육 이수 기록의 보관 기간').first()).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText('제9조 접속 기록의 보관 기간').first()).toBeVisible();
  await expect(result.getByRole('button', { name: /관련도가 낮은 결과 1개 더 보기/ })).toBeVisible();
  await expect(result.getByText('제5조 사내 동호회 활동비 지원')).toBeHidden();
  await expect(result).not.toContainText(/관련도\s+\d+%/);

  // <mark> uses bold + underline without background color fill.
  const firstMark = result.locator('mark').first();
  await expect(firstMark).toBeVisible();
  const markStyles = await firstMark.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { bg: cs.backgroundColor, decoration: cs.textDecorationLine };
  });
  expect(markStyles.bg).toBe('rgba(0, 0, 0, 0)');
  expect(markStyles.decoration).toContain('underline');

  // Evidence panel shows source file, page, surrounding passage, and [찾은 부분 복사하기] advances Step 3.
  const evidence = panel.getByRole('region', { name: workspaceCopy.evidence, exact: true });
  await expect(evidence).toContainText('인사_및_교육규정.docx');
  await expect(evidence).toContainText('1쪽');
  await page.evaluate(() => navigator.clipboard.writeText('-'));
  await evidence.getByRole('button', { name: evidenceCopy.copyPassage }).click();

  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain('인사_및_교육규정.docx');
  expect(copied).toContain('1쪽');
  expect(copied).toContain('보관 기간');
  await expect(panel.locator('[aria-current="step"]')).toContainText('내보내기');
  await expectNoAxeViolations(page);

  // Switch to outline tab -> inspect sections via /api/docindex/section (OS directory order independent).
  await result.getByRole('tab', { name: /목차/ }).click();
  await result.getByRole('button', { name: /제9조 접속 기록의 보관 기간/ }).click();
  await expect(result.getByText('접속 기록과 접근 이력의 보관 기간은 최소 3년')).toBeVisible({ timeout: 10_000 });
  await result.getByRole('button', { name: /제1장 총칙/ }).click();
  await expect(result.getByText('이 규정은 사내 직무 교육과 수료 기준에 관한 일반 원칙을 정한다.')).toBeVisible({ timeout: 10_000 });

  // Switch to tables tab -> run custom SQL via /api/docindex/table-sql.
  await result.getByRole('tab', { name: /문서 속 표/ }).click();
  await expect(result.getByRole('cell', { name: '정보보호팀' })).toBeVisible({ timeout: 10_000 });
  await result.getByRole('button', { name: resultCopy.tables.sqlSummary }).click();
  await result.getByRole('textbox', { name: resultCopy.tables.sqlLabel }).fill(
    "SELECT 항목, 책임_부서 FROM tbl WHERE 항목 = '승인 이력'",
  );
  await result.getByRole('button', { name: resultCopy.tables.runSql }).click();
  await expect(result.getByRole('cell', { name: '감사팀' })).toBeVisible({ timeout: 10_000 });
});

test('supports synonyms and document filter, blocks stale search on failed rebuild, and recovers expired index', async ({
  page,
}) => {
  const panel = await openSearchTab(page);
  await pickSearchFiles(panel, page, [EDUCATION_DOC]);

  const searchInput = panel.getByRole('searchbox', { name: resultCopy.searchForm.label });
  await expect(searchInput).toBeEnabled({ timeout: 20_000 });

  // Add second document with a synonym phrase ("보유 연한" via synonym "보유=보관") while build fails first.
  await page.route('**/api/docindex/build', (route) => route.abort('failed'));
  await addMoreSearchFiles(panel, page, [SECURITY_DOC]);

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  await expect(result.getByRole('alert')).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText(resultCopy.staleNotice)).toBeVisible();
  await expect(searchInput).toBeDisabled();

  // Unroute and recover via [다시 준비하기].
  await page.unroute('**/api/docindex/build');
  await result.getByRole('button', { name: resultCopy.retryPrepare }).click();
  await expect(searchInput).toBeEnabled({ timeout: 20_000 });

  // Configure synonym and filter to SECURITY_DOC via ListBox, then recover transparently from a 404 expired query.
  await panel.getByRole('button', { name: settingsCopy.heading }).click();
  await panel.getByRole('textbox', { name: settingsCopy.synonyms.label }).fill('보존=보관');

  const listRegion = panel.getByRole('region', { name: workspaceCopy.list, exact: true });
  await listRegion.getByRole('option', { name: /보안_및_자료관리규정\.docx/ }).click();

  let expiredOnce = false;
  await page.route('**/api/docindex/query', async (route) => {
    if (!expiredOnce) {
      expiredOnce = true;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: '문서 인덱스를 찾을 수 없다(만료되었거나 잘못된 index_id).' }),
      });
      return;
    }
    await route.continue();
  });

  await searchInput.fill('보존');
  await result.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();
  await expect(result.getByText('제9조 접속 기록의 보관 기간').first()).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText('제4조 교육 이수 기록의 보관 기간')).toHaveCount(0);

  // Switching scope to EDUCATION_DOC while viewing outline refreshes the outline body instead of leaking SECURITY_DOC.
  await result.getByRole('tab', { name: /목차/ }).click();
  await expect(result.getByText('접속 기록과 접근 이력의 보관 기간은 최소 3년')).toBeVisible({ timeout: 10_000 });
  await listRegion.getByRole('option', { name: /인사_및_교육규정\.docx/ }).click();
  await expect(result.getByText('이 규정은 사내 직무 교육과 수료 기준에 관한 일반 원칙을 정한다.')).toBeVisible({ timeout: 10_000 });
  await expect(result.getByText('접속 기록과 접근 이력의 보관 기간은 최소 3년')).toHaveCount(0);
});

test('rejects legacy .xls locally with actionable guidance and keeps axe clean on error', async ({
  page,
  pageLog,
}) => {
  const panel = await openSearchTab(page);
  const beforeRequests = pageLog.requests.length;

  const chooser = page.waitForEvent('filechooser');
  await panel.getByRole('button', { name: buttonCopy.pickFiles }).click();
  await (await chooser).setFiles([
    { name: '예전규정.xls', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('xls') },
  ]);

  const alert = panel.getByRole('region', { name: workspaceCopy.result, exact: true }).getByRole('alert');
  await expect(alert).toContainText(errorCopy.unsupportedFile.what);
  await expect(alert).toContainText(errorCopy.unsupportedFile.xlsWhy);
  expect(
    pageLog.requests.slice(beforeRequests).filter((u) => u.includes('/api/docindex/build')),
  ).toEqual([]);
  await expectNoAxeViolations(page);
});

test.describe('search tab at 320px wide', () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test('has no sideways scroll or axe violations on start and result screens', async ({ page }) => {
    const panel = await openSearchTab(page);
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);

    await pickSearchFiles(panel, page, [EDUCATION_DOC]);
    const searchInput = panel.getByRole('searchbox', { name: resultCopy.searchForm.label });
    await expect(searchInput).toBeEnabled({ timeout: 20_000 });
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);
  });
});

test('real-engine scoring keeps full matches primary, hides 2-gram fragments, shows full table truncation, and clears stale table on SQL error', async ({
  page,
}) => {
  const securityWithPw = buildDocxPayload('보안_및_비밀번호규정.docx', [
    {
      heading: '제9조 접속 기록의 보관 기간',
      body: '시스템 접속 기록과 접근 이력의 보관 기간은 최소 3년으로 정한다.',
      table: [
        ['항목', '보유 연한', '책임 부서'],
        ['접속 기록', '3년', '정보보호팀'],
      ],
    },
    {
      heading: '제12조 비밀번호 관리',
      body: '사용자 비밀번호는 90일마다 변경해야 하며 타인과 공유할 수 없다.',
    },
  ]);
  const receiptDoc = buildDocxPayload('지출_증빙규정.docx', [
    {
      heading: '제3조 지출 증빙 관리',
      body: '부서 운영비 지출 내역은 매월 정산하며 영수증 보관 기간은 3년으로 정한다. 마감 후 회계팀에 제출한다.',
    },
  ]);
  const telDoc = buildDocxPayload('내선_연락안내.docx', [
    {
      heading: '제2조 내선 번호 안내',
      body: '부서별 대표 내선 번호는 사내 포털에서 확인한다. 비상 연락 번호 변경 시 총무팀에 통보한다.',
    },
  ]);
  const largeRows: [string, string][] = [
    ['순번', '구분'],
    ...Array.from({ length: 210 }, (_, idx): [string, string] => [
      String(idx + 1),
      idx % 2 === 0 ? '정규' : '수시',
    ]),
  ];
  const approvalDoc = buildDocxPayload('결재_승인절차.docx', [
    {
      heading: '제6조 결재 승인 절차',
      body: '예산 집행 전 팀장 승인을 받아야 한다. 긴급 건은 사후 승인을 신청할 수 있다.',
      table: largeRows,
    },
  ]);

  const panel = await openSearchTab(page);
  await pickSearchFiles(panel, page, [
    EDUCATION_DOC,
    securityWithPw,
    CLUB_DOC,
    receiptDoc,
    telDoc,
    approvalDoc,
  ]);

  const searchInput = panel.getByRole('searchbox', { name: resultCopy.searchForm.label });
  await expect(searchInput).toBeEnabled({ timeout: 20_000 });

  // SettingsPanel topK input lets user clear and type "8" without snapping to "18".
  await panel.getByRole('button', { name: settingsCopy.heading }).click();
  const topKInput = panel.getByRole('spinbutton', { name: settingsCopy.topK.label });
  await topKInput.fill('');
  await topKInput.type('8');
  await expect(topKInput).toHaveValue('8');
  await topKInput.blur();
  await expect(topKInput).toHaveValue('8');

  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  const evidence = panel.getByRole('region', { name: workspaceCopy.evidence, exact: true });

  // Query 1: exact body match "영수증 보관 기간은 3년" stays primary even with low raw BM25, partial "보관" folds.
  await searchInput.fill('보관 기간');
  await result.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();
  await expect(result.getByText('제4조 교육 이수 기록의 보관 기간').first()).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText('제3조 지출 증빙 관리').first()).toBeVisible();
  await expect(result.getByText('제5조 사내 동호회 활동비 지원')).toBeHidden();

  // Query 2: "비밀번호 승인" keeps full-word hits primary, folds 2-gram-only "번호", and cleans displayed terms.
  await searchInput.fill('비밀번호 승인');
  await result.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();
  await expect(result.getByText('제12조 비밀번호 관리').first()).toBeVisible({ timeout: 20_000 });
  await expect(result.getByText('제6조 결재 승인 절차').first()).toBeVisible();
  await expect(result.getByText('제2조 내선 번호 안내')).toBeHidden();
  await expect(evidence).toContainText('일치한 단어: 비밀번호 ·');
  await expect(evidence).not.toContainText('비밀번호, 비밀');

  // Switch to tables tab: default SQL omits LIMIT 50, shows 200/210 truncation, and SQL 400 error clears old table rows.
  const capturedSqls: string[] = [];
  await page.route('**/api/docindex/table-sql', async (route) => {
    const body = route.request().postDataJSON() as { sql?: string };
    if (typeof body?.sql === 'string') capturedSqls.push(body.sql);
    await route.continue();
  });

  await result.getByRole('tab', { name: /문서 속 표/ }).click();
  const tableSelect = result.getByRole('combobox', { name: resultCopy.tables.selectLabel });
  await expect(tableSelect).toBeVisible({ timeout: 10_000 });

  // Open the small table first and confirm rows render.
  const secOption = await tableSelect.locator('option', { hasText: '보안_및_비밀번호규정.docx' }).getAttribute('value');
  const appOption = await tableSelect.locator('option', { hasText: '결재_승인절차.docx' }).getAttribute('value');
  expect(secOption).toBeTruthy();
  expect(appOption).toBeTruthy();

  await tableSelect.selectOption(secOption!);
  await expect(result.getByRole('cell', { name: '정보보호팀' })).toBeVisible({ timeout: 10_000 });

  // Switch to the 210-row table -> default SQL is SELECT * FROM tbl and shows 200-of-210 truncation.
  await tableSelect.selectOption(appOption!);
  await expect(result.getByText('총 210행 중 앞 200행만 보여 줘요')).toBeVisible({ timeout: 10_000 });
  expect(capturedSqls.every((sql) => sql === 'SELECT * FROM tbl')).toBe(true);
  await page.unroute('**/api/docindex/table-sql');

  // When switching back to secOption fails with 400, the previous 210-row table must not stay shown under the new header.
  await page.route('**/api/docindex/table-sql', (route) =>
    route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'SQL 구문 오류' }),
    }),
  );
  await tableSelect.selectOption(secOption!);
  const alert = result.getByRole('alert');
  await expect(alert).toContainText(searchErrorCopy.sqlFailed.what, { timeout: 10_000 });
  await expect(result.getByRole('cell', { name: '정규' })).toHaveCount(0);
  await expect(result.getByRole('button', { name: /엑셀용으로 복사하기/ })).toHaveCount(0);
});

test('cancels stale in-flight query on filter switch, clears old hits on failed re-search, and shares one rebuild on concurrent expiry', async ({
  page,
}) => {
  const panel = await openSearchTab(page);
  await pickSearchFiles(panel, page, [EDUCATION_DOC, SECURITY_DOC]);

  const searchInput = panel.getByRole('searchbox', { name: resultCopy.searchForm.label });
  await expect(searchInput).toBeEnabled({ timeout: 20_000 });

  const listRegion = panel.getByRole('region', { name: workspaceCopy.list, exact: true });
  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });

  // 1. Change doc filter while initial query is in flight -> filtered result wins, all-docs hit never appears.
  let releaseFirstQuery!: () => void;
  const firstQueryGate = new Promise<void>((resolve) => {
    releaseFirstQuery = resolve;
  });
  let firstQueryHeld = false;
  await page.route('**/api/docindex/query', async (route) => {
    const body = route.request().postDataJSON() as { doc_id?: string | null };
    if (!firstQueryHeld && !body?.doc_id) {
      firstQueryHeld = true;
      await firstQueryGate;
      await route.continue().catch(() => {});
      return;
    }
    await route.continue();
  });

  await searchInput.fill('보관 기간');
  await result.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();
  await expect.poll(() => firstQueryHeld).toBe(true);
  await listRegion.getByRole('option', { name: /보안_및_자료관리규정\.docx/ }).click();
  releaseFirstQuery();
  await expect(result.getByText('제9조 접속 기록의 보관 기간').first()).toBeVisible({ timeout: 10_000 });
  await expect(result.getByText('제4조 교육 이수 기록의 보관 기간')).toHaveCount(0);
  await page.unroute('**/api/docindex/query');

  // 2. If re-search fails after doc filter change, prior hits & related-table links must disappear.
  await page.route('**/api/docindex/query', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: '검색 서버 오류' }),
    }),
  );
  await listRegion.getByRole('option', { name: /인사_및_교육규정\.docx/ }).click();
  await expect(result.getByRole('alert')).toBeVisible({ timeout: 10_000 });
  await expect(result.getByText('제9조 접속 기록의 보관 기간')).toHaveCount(0);
  await expect(result.getByRole('button', { name: resultCopy.results.openTable })).toHaveCount(0);
  await page.unroute('**/api/docindex/query');

  // 3. Concurrent expiry while switching filter on outline tab with prior search results -> single rebuild recovers both.
  await searchInput.fill('보관 기간');
  await result.getByRole('button', { name: resultCopy.searchForm.submitButton }).click();
  await expect(result.getByText('제4조 교육 이수 기록의 보관 기간').first()).toBeVisible({ timeout: 10_000 });

  await result.getByRole('tab', { name: /목차/ }).click();
  await expect(result.getByText('이 규정은 사내 직무 교육과 수료 기준에 관한 일반 원칙을 정한다.')).toBeVisible({ timeout: 10_000 });

  let buildCalls = 0;
  let queryExpiredOnce = false;
  let sectionExpiredOnce = false;
  await page.route('**/api/docindex/build', async (route) => {
    buildCalls += 1;
    await route.continue();
  });
  await page.route('**/api/docindex/query', async (route) => {
    if (!queryExpiredOnce) {
      queryExpiredOnce = true;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: '문서 인덱스를 찾을 수 없다(만료되었거나 잘못된 index_id).' }),
      });
      return;
    }
    await route.continue();
  });
  await page.route('**/api/docindex/section', async (route) => {
    if (!sectionExpiredOnce) {
      sectionExpiredOnce = true;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: '문서 인덱스를 찾을 수 없다(만료되었거나 잘못된 index_id).' }),
      });
      return;
    }
    await route.continue();
  });

  await listRegion.getByRole('option', { name: /보안_및_자료관리규정\.docx/ }).click();
  await expect(result.getByText('시스템 접속 기록과 접근 이력의 보관 기간은 최소 3년')).toBeVisible({ timeout: 20_000 });
  await result.getByRole('tab', { name: /찾은 내용/ }).click();
  await expect(result.getByText('제9조 접속 기록의 보관 기간').first()).toBeVisible({ timeout: 10_000 });
  expect(buildCalls).toBe(1);
});
