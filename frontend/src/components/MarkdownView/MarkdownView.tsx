import { useMemo, type ReactNode } from 'react';
import { markdownCopy } from '../../copy/common.ts';
import { markdownToTree, type MdNode } from '../../lib/markdown.ts';
import styles from './MarkdownView.module.css';

export interface MarkdownViewProps {
  /** Markdown text. Raw HTML in it is shown as text, never run. */
  source: string;
  className?: string;
}

/**
 * Shows markdown as a document. It renders the safe tree from
 * lib/markdown.ts as React elements; there is no dangerouslySetInnerHTML.
 * Links open in a new window without a referrer; images show as alt text.
 */
export function MarkdownView({ source, className }: MarkdownViewProps) {
  const tree = useMemo(() => markdownToTree(source), [source]);
  return <div className={[styles.markdown, className ?? ''].join(' ').trim()}>{renderNodes(tree)}</div>;
}

function renderNodes(nodes: readonly MdNode[]): ReactNode[] {
  return nodes.map((node, index) => renderNode(node, index));
}

function renderNode(node: MdNode, key: number): ReactNode {
  if (node.type === 'text') return node.value;
  const children = node.children.length > 0 ? renderNodes(node.children) : undefined;
  const { tag, attrs } = node;
  switch (tag) {
    case 'a':
      return (
        <a key={key} href={attrs.href} title={attrs.title} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      );
    case 'ol':
      return (
        <ol key={key} start={attrs.start}>
          {children}
        </ol>
      );
    case 'th':
    case 'td': {
      const Cell = tag;
      return (
        <Cell key={key} className={attrs.align ? styles[`align-${attrs.align}`] : undefined}>
          {children}
        </Cell>
      );
    }
    case 'table':
      return (
        <div key={key} className={styles.tableScroll} role="region" aria-label={markdownCopy.tableRegion} tabIndex={0}>
          <table>{children}</table>
        </div>
      );
    case 'hr':
      return <hr key={key} />;
    case 'br':
      return <br key={key} />;
    default: {
      const Tag = tag;
      return <Tag key={key}>{children}</Tag>;
    }
  }
}
