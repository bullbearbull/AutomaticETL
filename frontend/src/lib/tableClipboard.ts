/**
 * Table copy for Excel: tab-separated text plus an HTML <table>, so a paste
 * into Excel splits cells the way the table shows them. Text cells that a
 * spreadsheet could run as a formula are guarded on the way out: the TSV puts
 * an apostrophe in front (see `spreadsheetSafe`), and the HTML table keeps the
 * text as it is and marks the cell as an Excel text cell, because spreadsheets
 * such as Numbers show that apostrophe when they paste HTML. The table on
 * screen is not changed.
 */

export type TableCell = string | number | boolean | null | undefined;
export type TableRows = readonly (readonly TableCell[])[];

function cellText(cell: TableCell): string {
  if (cell === null || cell === undefined) return '';
  return String(cell);
}

/** First characters that make a spreadsheet read a cell as a formula. */
const FORMULA_START = /^[=+\-@\t\r]/;
/**
 * A plain number such as -1,234, +5%, -3.5 or -.5 stays a number. The
 * exception is kept narrow on purpose: exponent notation (-1e5) is not in it.
 */
const PLAIN_NUMBER = /^[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)%?$/;

/**
 * Puts an apostrophe in front of text that starts with =, +, -, @, a tab or a
 * CR, so a spreadsheet keeps it as text instead of running it as a formula.
 * Plain numbers and a lone "-" (often used for an empty cell) stay as they
 * are. The CSV files the server writes use the same rule
 * (`spreadsheet_safe_cell` in `src/autoetl/parser/exporters.py`).
 */
export function spreadsheetSafe(text: string): string {
  return startsLikeFormula(text) ? `'${text}` : text;
}

/** True when `spreadsheetSafe` would guard this text. */
function startsLikeFormula(text: string): boolean {
  return FORMULA_START.test(text) && text !== '-' && !PLAIN_NUMBER.test(text);
}

/** Cell text for the TSV. Only text cells can hold a formula. */
function copyCellText(cell: TableCell): string {
  return typeof cell === 'string' ? spreadsheetSafe(cell) : cellText(cell);
}

/** Excel reads a cell with this number format as text, so it never runs it. */
const EXCEL_TEXT_CELL = ` style="mso-number-format:'\\@'"`;

/**
 * Rows → TSV. A cell that holds a tab, a line break or a double quote is
 * wrapped in double quotes with inner quotes doubled, which Excel reads back
 * as one cell. Lines end with CRLF, as Excel writes them.
 */
export function rowsToTsv(rows: TableRows): string {
  return rows
    .map((row) =>
      row
        .map((cell) => {
          const text = copyCellText(cell).replace(/\r\n?/g, '\n');
          return /[\t\n"]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
        })
        .join('\t'),
    )
    .join('\r\n');
}

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Rows → an escaped HTML `<table>`. The first `headerRows` rows become
 * `<th>` cells in `<thead>`. Line breaks inside a cell become `<br>`. A text
 * cell that starts like a formula keeps its text and gets the Excel text
 * format instead of an apostrophe.
 */
export function rowsToHtml(rows: TableRows, options: { headerRows?: number } = {}): string {
  const headerRows = Math.max(0, Math.min(options.headerRows ?? 0, rows.length));
  const renderCell = (cell: TableCell, tag: 'th' | 'td') => {
    const guard = typeof cell === 'string' && startsLikeFormula(cell) ? EXCEL_TEXT_CELL : '';
    return `<${tag}${guard}>${escapeHtml(cellText(cell)).replace(/\r\n?|\n/g, '<br>')}</${tag}>`;
  };
  const renderRow = (row: readonly TableCell[], tag: 'th' | 'td') =>
    `<tr>${row.map((cell) => renderCell(cell, tag)).join('')}</tr>`;
  const head = rows.slice(0, headerRows).map((row) => renderRow(row, 'th')).join('');
  const body = rows.slice(headerRows).map((row) => renderRow(row, 'td')).join('');
  return `<table>${head ? `<thead>${head}</thead>` : ''}<tbody>${body}</tbody></table>`;
}

interface ClipboardLike {
  write?: (items: ClipboardItem[]) => Promise<void>;
  writeText?: (text: string) => Promise<void>;
}

type ClipboardItemConstructor = new (items: Record<string, Blob>) => ClipboardItem;

export interface CopyTableOptions {
  headerRows?: number;
  /** Defaults to `navigator.clipboard`. */
  clipboard?: ClipboardLike | undefined;
  /** Defaults to the global `ClipboardItem`. */
  ClipboardItemImpl?: ClipboardItemConstructor | undefined;
}

/**
 * Writes the table to the clipboard as `text/plain` (TSV) and `text/html`.
 * Falls back to `writeText` with the TSV when rich copy is not available.
 * Resolves to which way worked; rejects when neither did.
 */
export async function copyTable(rows: TableRows, options: CopyTableOptions = {}): Promise<'rich' | 'plain'> {
  const clipboard = options.clipboard ?? globalThis.navigator?.clipboard;
  const ItemImpl =
    options.ClipboardItemImpl ??
    (globalThis as { ClipboardItem?: ClipboardItemConstructor }).ClipboardItem;
  const tsv = rowsToTsv(rows);
  if (!clipboard) throw new Error('클립보드를 쓸 수 없어요.');

  if (ItemImpl && typeof clipboard.write === 'function') {
    try {
      const html = rowsToHtml(rows, { headerRows: options.headerRows ?? 0 });
      const item = new ItemImpl({
        'text/plain': new Blob([tsv], { type: 'text/plain' }),
        'text/html': new Blob([html], { type: 'text/html' }),
      });
      await clipboard.write([item]);
      return 'rich';
    } catch {
      // Fall through to plain text.
    }
  }
  if (typeof clipboard.writeText === 'function') {
    await clipboard.writeText(tsv);
    return 'plain';
  }
  throw new Error('클립보드를 쓸 수 없어요.');
}
