import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  MAX_UPLOAD_BYTES,
  base64FromDataUrl,
  checkFile,
  fileExtension,
  formatBytes,
  readFileAsBase64,
} from '../../src/lib/files.ts';

test('fileExtension returns the lower-case extension with the dot', () => {
  assert.equal(fileExtension('보고서.PDF'), '.pdf');
  assert.equal(fileExtension('archive.tar.gz'), '.gz');
  assert.equal(fileExtension('README'), '');
  assert.equal(fileExtension('.hidden'), '');
  assert.equal(fileExtension('끝점.'), '');
  assert.equal(fileExtension('C:\\폴더.v2\\파일'), '');
});

test('formatBytes uses units without a space', () => {
  assert.equal(formatBytes(0), '0B');
  assert.equal(formatBytes(1023), '1023B');
  assert.equal(formatBytes(1536), '1.5KB');
  assert.equal(formatBytes(12 * 1024), '12KB');
  assert.equal(formatBytes(MAX_UPLOAD_BYTES), '50MB');
  assert.equal(formatBytes(3.45 * 1024 * 1024), '3.5MB');
  assert.equal(formatBytes(-1), '');
});

test('checkFile reports the extension before the size', () => {
  const allowed = ['.pdf', '.xlsx'];
  assert.deepEqual(checkFile({ name: 'a.pdf', size: 10 }, { allowedExtensions: allowed }), {
    ok: true,
    extension: '.pdf',
  });
  assert.deepEqual(checkFile({ name: 'a.xls', size: MAX_UPLOAD_BYTES + 1 }, { allowedExtensions: allowed }), {
    ok: false,
    reason: 'extension',
    extension: '.xls',
  });
  assert.deepEqual(checkFile({ name: 'a.xlsx', size: MAX_UPLOAD_BYTES + 1 }, { allowedExtensions: allowed }), {
    ok: false,
    reason: 'size',
    extension: '.xlsx',
    size: MAX_UPLOAD_BYTES + 1,
    maxBytes: MAX_UPLOAD_BYTES,
  });
  assert.equal(checkFile({ name: 'a.pdf', size: MAX_UPLOAD_BYTES }, { allowedExtensions: allowed }).ok, true);
  assert.equal(checkFile({ name: 'a.pdf', size: 11 }, { allowedExtensions: allowed, maxBytes: 10 }).ok, false);
});

test('base64FromDataUrl keeps only the payload', () => {
  assert.equal(base64FromDataUrl('data:application/pdf;base64,JVBERi0='), 'JVBERi0=');
  assert.equal(base64FromDataUrl('data:;base64,'), '');
  assert.throws(() => base64FromDataUrl('data:text/plain,hello'));
  assert.throws(() => base64FromDataUrl('hello'));
});

/** A FileReader stand-in: Node has Blob but no FileReader. */
class FakeReader {
  result: string | ArrayBuffer | null = null;
  error: unknown = null;
  onload: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onabort: ((event: unknown) => void) | null = null;
  aborted = false;
  readAsDataURL(blob: Blob): void {
    void blob.arrayBuffer().then((buffer) => {
      if (this.aborted) return;
      this.result = `data:${blob.type || 'application/octet-stream'};base64,${Buffer.from(buffer).toString('base64')}`;
      this.onload?.({});
    });
  }
  abort(): void {
    this.aborted = true;
    this.onabort?.({});
  }
}

test('readFileAsBase64 reads through readAsDataURL', async () => {
  const blob = new Blob(['한글 내용'], { type: 'text/plain' });
  const base64 = await readFileAsBase64(blob, { createReader: () => new FakeReader() });
  assert.equal(Buffer.from(base64, 'base64').toString('utf8'), '한글 내용');
});

test('readFileAsBase64 rejects when aborted', async () => {
  const controller = new AbortController();
  const promise = readFileAsBase64(new Blob(['x']), {
    signal: controller.signal,
    createReader: () => new FakeReader(),
  });
  controller.abort();
  await assert.rejects(promise, (error: unknown) => (error as Error).name === 'AbortError');

  const already = new AbortController();
  already.abort();
  await assert.rejects(readFileAsBase64(new Blob(['x']), { signal: already.signal, createReader: () => new FakeReader() }));
});
