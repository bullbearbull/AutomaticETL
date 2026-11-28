/**
 * The `test` every spec uses. Two guards run around each test:
 *
 * - CSP: a `securitypolicyviolation` listener, installed before any page
 *   script runs, must collect nothing.
 * - Network: every request the page makes stays on the local server
 *   (`data:` and `blob:` URLs are allowed).
 *
 * Tests read `requests` to check which URLs the page asked for.
 */
import { test as base, expect } from '@playwright/test';
import { ORIGIN } from './server.ts';

export interface CspViolation {
  directive: string;
  blockedURI: string;
  sourceFile: string;
}

export interface PageLog {
  /** Every URL the page requested, in order. */
  requests: string[];
  cspViolations: CspViolation[];
}

function isLocal(url: string): boolean {
  return url.startsWith(`${ORIGIN}/`) || url.startsWith('data:') || url.startsWith('blob:');
}

export const test = base.extend<{ pageLog: PageLog }>({
  pageLog: [
    async ({ page }, use) => {
      const log: PageLog = { requests: [], cspViolations: [] };
      page.on('request', (request) => log.requests.push(request.url()));
      await page.exposeFunction('__reportCspViolation', (violation: CspViolation) => {
        log.cspViolations.push(violation);
      });
      await page.addInitScript(() => {
        document.addEventListener('securitypolicyviolation', (event) => {
          const report = (window as unknown as { __reportCspViolation: (v: unknown) => void }).__reportCspViolation;
          report({ directive: event.violatedDirective, blockedURI: event.blockedURI, sourceFile: event.sourceFile });
        });
      });

      await use(log);

      expect(log.cspViolations, 'Content-Security-Policy violations').toEqual([]);
      expect(log.requests.filter((url) => !isLocal(url)), 'requests that left the local server').toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
