import { useEffect, useRef, useState } from 'react';
import { Button, Details, Icon } from '../../components/index.ts';
import type { AppError } from '../../lib/errors.ts';
import type {
  DocIndexBuildResult,
  DocSectionContent,
  EvidenceHitInfo,
  IndexedTableInfo,
  SearchEvidencePack,
} from './api.ts';
import { evidenceCopy, listCopy } from './copy.ts';
import { baseFileName, formatPageRange, pageUnitFor, SUPPORTED_DOCUMENTS_LABEL } from './formats.ts';
import {
  analyzeHitMatch,
  collectHighlightTerms,
  extractSurroundingPassage,
  formatCitationCopyText,
} from './highlight.ts';
import { HighlightedText } from './HighlightedText.tsx';
import type { SearchSubTabId } from './ResultPanel.tsx';
import styles from './EvidencePanel.module.css';

export interface EvidencePanelProps {
  index: DocIndexBuildResult | null;
  error: AppError | null;
  fileRevision: number;
  activeTab: SearchSubTabId;
  searchResult: SearchEvidencePack | null;
  selectedHit: EvidenceHitInfo | null;
  sectionContent: DocSectionContent | null;
  selectedTable: IndexedTableInfo | null;
  onCopied: (fileRevision: number) => void;
}

const FEEDBACK_MS = 6000;

/**
 * Right column (`근거`) for the search tab:
 * - On error before ready: supported document formats list
 * - When a search hit is selected: exact source, page, reasoning, surrounding-sentence
 *   passage, and primary [찾은 부분 복사하기] button (copying doc name + page + excerpt)
 * - In `목차` / `문서 속 표` tabs: matching provenance summary and section copy button
 */
export function EvidencePanel({
  index,
  error,
  fileRevision,
  activeTab,
  searchResult,
  selectedHit,
  sectionContent,
  selectedTable,
  onCopied,
}: EvidencePanelProps) {
  const [showFull, setShowFull] = useState(false);
  const [copyMessage, setCopyMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  useEffect(() => {
    setShowFull(false);
    setCopyMessage(null);
  }, [selectedHit?.sectionId, sectionContent?.section.sectionId, activeTab]);

  if (error && !index) {
    return (
      <div className={styles.root}>
        <section className={styles.section}>
          <h3 className={styles.title}>{listCopy.unsupportedFormatsHeading}</h3>
          <p className={styles.bodyText}>{SUPPORTED_DOCUMENTS_LABEL}</p>
        </section>
      </div>
    );
  }

  if (!index) return null;

  const docFormatById = new Map(index.documents.map((d) => [d.docId, d.format]));

  const copyWithFeedback = async (payloadText: string, fileName: string, pageLabel: string) => {
    const startedRevision = fileRevision;
    window.clearTimeout(timerRef.current);
    setCopyMessage(null);
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(payloadText);
      setCopyMessage({
        text: evidenceCopy.copiedAnnouncement(fileName, pageLabel),
        ok: true,
      });
      onCopied(startedRevision);
    } catch {
      setCopyMessage({ text: evidenceCopy.copyFailed, ok: false });
    }
    timerRef.current = window.setTimeout(() => setCopyMessage(null), FEEDBACK_MS);
  };

  if (activeTab === 'results' && selectedHit && searchResult) {
    const fileName = baseFileName(selectedHit.sourcePath);
    const unit = pageUnitFor(docFormatById.get(selectedHit.docId) ?? fileName);
    const pageLabel = formatPageRange(selectedHit.pageStart, selectedHit.pageEnd, unit);
    const sectionTitle =
      selectedHit.sectionPath.length > 0 ? selectedHit.sectionPath.join(' › ') : selectedHit.title;
    const matchInfo = analyzeHitMatch(selectedHit, searchResult.query);
    const terms = collectHighlightTerms(searchResult.query, selectedHit.matchedTerms);
    const surrounding = extractSurroundingPassage(selectedHit.fullText, terms, {
      maxChars: 900,
      contextSentences: 2,
    });
    const canExpand = surrounding.truncatedBefore || surrounding.truncatedAfter;
    const displayedText = showFull || !canExpand ? selectedHit.fullText : surrounding.passage;
    const citationText = formatCitationCopyText({
      fileName,
      pageLabel,
      sectionTitle,
      passage: displayedText,
    });

    return (
      <div className={styles.root}>
        <section className={styles.section}>
          <h3 className={styles.title}>{evidenceCopy.selectedHeading}</h3>
          <dl className={styles.facts}>
            <div>
              <dt>{evidenceCopy.labels.file}</dt>
              <dd>{fileName}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.location}</dt>
              <dd className="tabular-nums">{pageLabel}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.section}</dt>
              <dd>{sectionTitle}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.reason}</dt>
              <dd>
                {evidenceCopy.reasonText(
                  matchInfo,
                  selectedHit.outlineBoost,
                  selectedHit.rerankScore,
                )}
              </dd>
            </div>
          </dl>
        </section>

        <section className={styles.section}>
          <div className={styles.passageHeader}>
            <h4 className={styles.subtitle}>{evidenceCopy.passageHeading}</h4>
            {canExpand ? (
              <Button variant="quiet" onPress={() => setShowFull((prev) => !prev)}>
                {showFull ? evidenceCopy.showPassageOnly : evidenceCopy.showFullSection}
              </Button>
            ) : null}
          </div>
          <p className={styles.passageBox}>
            <HighlightedText text={displayedText} terms={terms} />
          </p>
          <Button
            variant="primary"
            onPress={() => void copyWithFeedback(citationText, fileName, pageLabel)}
            className={styles.copyBtn}
          >
            {evidenceCopy.copyPassage}
          </Button>
          <p role="status" className={styles.copyFeedback}>
            {copyMessage ? (
              <>
                <Icon
                  name={copyMessage.ok ? 'check' : 'warning'}
                  className={copyMessage.ok ? styles.okIcon : styles.warnIcon}
                />
                <span>{copyMessage.text}</span>
              </>
            ) : null}
          </p>
        </section>

        <Details summary={evidenceCopy.detailsSummary}>
          <dl className={`${styles.facts} ${styles.techFacts} tabular-nums`}>
            <div>
              <dt>{evidenceCopy.techLabels.sectionId}</dt>
              <dd>{selectedHit.sectionId}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.techLabels.scores}</dt>
              <dd>{`${selectedHit.finalScore.toFixed(3)} / ${selectedHit.bm25Score.toFixed(4)}`}</dd>
            </div>
            {selectedHit.rerankScore !== null ? (
              <div>
                <dt>{evidenceCopy.techLabels.rerankScore}</dt>
                <dd>{selectedHit.rerankScore.toFixed(3)}</dd>
              </div>
            ) : null}
            <div>
              <dt>{evidenceCopy.techLabels.mode}</dt>
              <dd>{evidenceCopy.techLabels.modeValue(searchResult.mode)}</dd>
            </div>
          </dl>
        </Details>
      </div>
    );
  }

  if (activeTab === 'outline' && sectionContent) {
    const fileName = baseFileName(sectionContent.section.sourcePath);
    const unit = pageUnitFor(docFormatById.get(sectionContent.section.docId) ?? fileName);
    const pageLabel = formatPageRange(
      sectionContent.section.pageStart,
      sectionContent.section.pageEnd,
      unit,
    );
    const sectionTitle =
      sectionContent.section.sectionPath.length > 0
        ? sectionContent.section.sectionPath.join(' › ')
        : sectionContent.section.title;
    const citationText = formatCitationCopyText({
      fileName,
      pageLabel,
      sectionTitle,
      passage: sectionContent.fullText,
    });

    return (
      <div className={styles.root}>
        <section className={styles.section}>
          <h3 className={styles.title}>{evidenceCopy.outlineHeading}</h3>
          <dl className={styles.facts}>
            <div>
              <dt>{evidenceCopy.labels.file}</dt>
              <dd>{fileName}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.location}</dt>
              <dd className="tabular-nums">{pageLabel}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.section}</dt>
              <dd>{sectionTitle}</dd>
            </div>
          </dl>
          <Button
            variant="primary"
            onPress={() => void copyWithFeedback(citationText, fileName, pageLabel)}
            className={styles.copyBtn}
          >
            {evidenceCopy.copySection}
          </Button>
          <p role="status" className={styles.copyFeedback}>
            {copyMessage ? (
              <>
                <Icon
                  name={copyMessage.ok ? 'check' : 'warning'}
                  className={copyMessage.ok ? styles.okIcon : styles.warnIcon}
                />
                <span>{copyMessage.text}</span>
              </>
            ) : null}
          </p>
        </section>
      </div>
    );
  }

  if (activeTab === 'tables' && selectedTable) {
    const fileName = baseFileName(selectedTable.sourcePath);
    const unit = pageUnitFor(docFormatById.get(selectedTable.docId) ?? fileName);
    return (
      <div className={styles.root}>
        <section className={styles.section}>
          <h3 className={styles.title}>{evidenceCopy.tableHeading}</h3>
          <dl className={styles.facts}>
            <div>
              <dt>{evidenceCopy.labels.file}</dt>
              <dd>{fileName}</dd>
            </div>
            <div>
              <dt>{evidenceCopy.labels.location}</dt>
              <dd className="tabular-nums">{`${selectedTable.page}${unit}`}</dd>
            </div>
            <div>
              <dt>크기</dt>
              <dd className="tabular-nums">{`${selectedTable.rowCount.toLocaleString('ko-KR')}행 × ${selectedTable.colCount}열`}</dd>
            </div>
          </dl>
          <p className={styles.bodyText}>{evidenceCopy.tableTip}</p>
        </section>
      </div>
    );
  }

  return (
    <div className={styles.root}>
      <section className={styles.section}>
        <h3 className={styles.title}>{evidenceCopy.idleTitle}</h3>
        <p className={styles.bodyText}>{evidenceCopy.idleBody}</p>
        <ul className={styles.factList}>
          {evidenceCopy
            .idleFacts(
              index.summary.documentsIndexed,
              index.summary.sectionsIndexed,
              index.summary.tablesIndexed,
            )
            .map((line, i) => (
              <li key={i}>{line}</li>
            ))}
        </ul>
      </section>
    </div>
  );
}
