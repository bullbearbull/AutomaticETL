/**
 * Markdown → a small, safe node tree. markdown-it parses with `html: false`
 * (raw HTML stays text), and the tree only holds an allow-list of tags and
 * attributes. MarkdownView maps the tree to React elements, so nothing is
 * ever inserted as HTML. Images become their alt text, so a document can
 * never make the page load an external resource.
 */
import MarkdownIt, { type MarkdownIt as MarkdownItInstance, type Token } from 'markdown-it';
import { markdownCopy } from '../copy/common.ts';

export type MdTag =
  | 'p'
  | 'h1'
  | 'h2'
  | 'h3'
  | 'h4'
  | 'h5'
  | 'h6'
  | 'blockquote'
  | 'ul'
  | 'ol'
  | 'li'
  | 'pre'
  | 'code'
  | 'em'
  | 'strong'
  | 's'
  | 'a'
  | 'span'
  | 'hr'
  | 'br'
  | 'table'
  | 'thead'
  | 'tbody'
  | 'tr'
  | 'th'
  | 'td';

export interface MdAttrs {
  href?: string;
  title?: string;
  start?: number;
  align?: 'left' | 'center' | 'right';
}

export interface MdElement {
  type: 'element';
  tag: MdTag;
  attrs: MdAttrs;
  children: MdNode[];
}

export interface MdText {
  type: 'text';
  value: string;
}

export type MdNode = MdElement | MdText;

const ALLOWED_TAGS: ReadonlySet<string> = new Set<MdTag>([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code',
  'em', 'strong', 's', 'a', 'span', 'hr', 'br', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
]);

const SAFE_SCHEMES: ReadonlySet<string> = new Set(['http', 'https', 'mailto']);

let parser: MarkdownItInstance | undefined;

function getParser(): MarkdownItInstance {
  parser ??= new MarkdownIt({ html: false, linkify: false, typographer: false, breaks: false });
  return parser;
}

/**
 * Returns the link target when it is safe to put in `href`, otherwise
 * `undefined`. Allowed: http, https, mailto, and scheme-less links
 * (`#part`, `/path`, `file.md`). Control characters and spaces are removed
 * before the scheme check, so `java\tscript:` is caught too.
 */
export function safeHref(raw: string | null | undefined): string | undefined {
  if (raw === null || raw === undefined) return undefined;
  const cleaned = raw.replace(/[\u0000- \u007f-\u009f]/g, '');
  if (cleaned === '') return undefined;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(cleaned);
  if (scheme) {
    return SAFE_SCHEMES.has(scheme[1]!.toLowerCase()) ? raw.trim() : undefined;
  }
  // `//host/path` is protocol-relative: it would leave this PC's page.
  if (cleaned.startsWith('//') || cleaned.startsWith('\\\\') || cleaned.startsWith('/\\')) return undefined;
  return raw.trim();
}

function alignFromStyle(style: string | null): MdAttrs['align'] {
  const match = /text-align:\s*(left|center|right)/.exec(style ?? '');
  return (match?.[1] as MdAttrs['align']) ?? undefined;
}

/** An attribute as text (markdown-it types allow numbers too). */
function attrText(token: Token, name: string): string | null {
  const value = token.attrGet(name);
  return value === null ? null : String(value);
}

interface Frame {
  /** `null` marks a transparent wrapper (for example a hidden paragraph in a tight list). */
  element: MdElement | null;
  children: MdNode[];
}

function openFrame(stack: Frame[], token: Token, tag: string): void {
  if (!ALLOWED_TAGS.has(tag) || token.hidden) {
    stack.push({ element: null, children: [] });
    return;
  }
  const attrs: MdAttrs = {};
  if (tag === 'ol') {
    const start = Number(token.attrGet('start'));
    if (Number.isInteger(start) && start !== 1) attrs.start = start;
  }
  if (tag === 'th' || tag === 'td') {
    const align = alignFromStyle(attrText(token, 'style'));
    if (align) attrs.align = align;
  }
  if (tag === 'a') {
    const href = safeHref(attrText(token, 'href'));
    const title = attrText(token, 'title');
    if (title) attrs.title = title;
    if (href === undefined) {
      const element: MdElement = { type: 'element', tag: 'span', attrs: {}, children: [] };
      stack.push({ element, children: element.children });
      return;
    }
    attrs.href = href;
  }
  const element: MdElement = { type: 'element', tag: tag as MdTag, attrs, children: [] };
  stack.push({ element, children: element.children });
}

function closeFrame(stack: Frame[]): void {
  if (stack.length <= 1) return;
  const frame = stack.pop()!;
  const parent = stack[stack.length - 1]!;
  if (frame.element) parent.children.push(frame.element);
  else parent.children.push(...frame.children);
}

function pushText(stack: Frame[], value: string): void {
  if (value === '') return;
  const children = stack[stack.length - 1]!.children;
  const last = children[children.length - 1];
  if (last?.type === 'text') last.value += value;
  else children.push({ type: 'text', value });
}

function pushElement(stack: Frame[], tag: MdTag, children: MdNode[] = []): void {
  stack[stack.length - 1]!.children.push({ type: 'element', tag, attrs: {}, children });
}

function walk(tokens: readonly Token[], stack: Frame[]): void {
  for (const token of tokens) {
    if (token.nesting === 1) {
      openFrame(stack, token, token.tag);
      continue;
    }
    if (token.nesting === -1) {
      closeFrame(stack);
      continue;
    }
    switch (token.type) {
      case 'inline':
        walk(token.children ?? [], stack);
        break;
      case 'text':
      case 'html_inline':
      case 'html_block':
        pushText(stack, token.content);
        break;
      case 'softbreak':
        pushText(stack, '\n');
        break;
      case 'hardbreak':
        pushElement(stack, 'br');
        break;
      case 'code_inline':
        pushElement(stack, 'code', [{ type: 'text', value: token.content }]);
        break;
      case 'fence':
      case 'code_block':
        pushElement(stack, 'pre', [
          { type: 'element', tag: 'code', attrs: {}, children: [{ type: 'text', value: token.content }] },
        ]);
        break;
      case 'hr':
        pushElement(stack, 'hr');
        break;
      case 'image':
        pushText(stack, markdownCopy.image(token.content));
        break;
      default:
        if (token.content) pushText(stack, token.content);
    }
  }
}

/** Parses markdown into the safe node tree that MarkdownView renders. */
export function markdownToTree(source: string): MdNode[] {
  const tokens = getParser().parse(source, {});
  const root: Frame = { element: null, children: [] };
  const stack: Frame[] = [root];
  walk(tokens, stack);
  while (stack.length > 1) closeFrame(stack);
  return root.children;
}
