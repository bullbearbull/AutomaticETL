import { defineConfig, devices } from '@playwright/test';
import { ORIGIN, PORT } from './e2e/support/server.ts';

/**
 * Browser tests against the real local server (`autoetl ui`), which serves
 * the built screen from src/autoetl/ui/static/ at /next/. Run
 * `npm run build` first so the server has the current build.
 */
export default defineConfig({
  testDir: './e2e',
  // The server keeps the last 10 ZIPs for all clients, so tests run one at a time.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: ORIGIN,
    trace: 'off',
    locale: 'ko-KR',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run --frozen autoetl ui --port ${PORT} --no-browser`,
    cwd: '..',
    url: `${ORIGIN}/next/`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    stdout: 'pipe',
    stderr: 'pipe',
    // The server prints Korean. Keep its output UTF-8 whatever the locale.
    env: { LANG: 'C.UTF-8', PYTHONIOENCODING: 'utf-8' },
  },
});
