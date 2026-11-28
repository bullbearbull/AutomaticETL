import { Icon } from '../../components/index.ts';
import type { AppError } from '../../lib/errors.ts';
import type { AnalyzeResult } from './api.ts';
import { evidenceCopy, maskingCopy } from './copy.ts';
import type { ColumnOverrideDraft } from './overrides.ts';
import { extractPiiColumns, unmaskedPiiColumns } from './pii.ts';
import styles from './EvidencePanel.module.css';

export interface EvidencePanelProps {
  analysis: AnalyzeResult | null;
  selectedTable: string | null;
  overrides: Readonly<Record<string, ColumnOverrideDraft>>;
  error: AppError | null;
}

/**
 * Right column (`근거`) for the DB tab:
 * - On blocking error: supported format reference
 * - On analyzed table: why PII columns were flagged (column name, type, value
 *   shape mask — never raw cell values) + masked/hashed preview, plus primary
 *   key reasoning and trial-load validation status.
 */
export function EvidencePanel({ analysis, selectedTable, overrides, error }: EvidencePanelProps) {
  if (error && !analysis) {
    return (
      <div className={styles.root}>
        <h2 className={styles.title}>{evidenceCopy.formatsHeading}</h2>
        <p className={styles.note}>{evidenceCopy.formatsNote}</p>
      </div>
    );
  }

  if (!analysis) return null;

  const currentTable =
    analysis.tables.find((t) => t.source.tableName === selectedTable) ?? analysis.tables[0] ?? null;
  if (!currentTable) return null;

  const tablePii = extractPiiColumns(analysis, overrides).filter(
    (item) => item.table === currentTable.source.tableName,
  );
  const otherUnmaskedTables = Array.from(
    new Set(
      unmaskedPiiColumns(analysis, overrides)
        .filter((item) => item.table !== currentTable.source.tableName)
        .map((item) => item.table),
    ),
  );

  const pkInfo = analysis.relations.primaryKeys[currentTable.source.tableName];
  const scorecard =
    analysis.loadPlan.scorecards.find(
      (sc) => sc.candidateId === analysis.loadPlan.selectedCandidateId,
    ) ?? analysis.loadPlan.scorecards[0];

  const allWarnings = [...analysis.warnings, ...currentTable.warnings];

  return (
    <div className={styles.root}>
      <section className={styles.section}>
        <h2 className={styles.title}>{evidenceCopy.piiHeading}</h2>
        {tablePii.length === 0 ? (
          <p className={styles.note}>{evidenceCopy.piiNoneInTable}</p>
        ) : (
          <ul className={styles.piiList}>
            {tablePii.map((item) => {
              const isSafe = item.effectiveMasking !== 'none';
              return (
                <li key={item.sourceColumn} className={styles.piiCard}>
                  <div className={styles.piiHeader}>
                    <span className={styles.piiColName}>{item.sourceColumn}</span>
                    <span className={isSafe ? styles.statusSafe : styles.statusWarn}>
                      <Icon name={isSafe ? 'lock' : 'warning'} />
                      {maskingCopy[item.effectiveMasking].label}
                    </span>
                  </div>
                  <dl className={styles.piiFacts}>
                    <div>
                      <dt>{evidenceCopy.piiReasonLabel}</dt>
                      <dd>
                        {evidenceCopy.piiReasonText(
                          item.sourceColumn,
                          item.typeLabels,
                          item.level,
                          item.patternMask,
                          item.recommendation,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{evidenceCopy.piiPreviewLabel}</dt>
                      <dd className={styles.previewText}>
                        {evidenceCopy.piiPreviewValue(
                          item.effectiveMasking,
                          item.maskExample,
                          item.hashExample,
                        )}
                      </dd>
                    </div>
                  </dl>
                </li>
              );
            })}
          </ul>
        )}
        {otherUnmaskedTables.length > 0 ? (
          <p className={styles.crossWarn}>
            <Icon name="warning" className={styles.crossWarnIcon} />
            <span>{evidenceCopy.piiInOtherTables(otherUnmaskedTables)}</span>
          </p>
        ) : null}
      </section>

      <section className={styles.section}>
        <h3 className={styles.subtitle}>{evidenceCopy.pkHeading}</h3>
        {pkInfo && pkInfo.columns.length > 0 ? (
          <>
            <p className={styles.pkColumns}>{pkInfo.columns.join(', ')}</p>
            {pkInfo.reasons.length > 0 ? (
              <ul className={styles.rawList}>
                {pkInfo.reasons.map((reason, idx) => (
                  <li key={idx}>{reason}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          <p className={styles.note}>{evidenceCopy.pkNone}</p>
        )}

        {scorecard && scorecard.trialLoadExecuted ? (
          <p className={scorecard.trialLoadOk ? styles.checkOk : styles.checkWarn}>
            <Icon name={scorecard.trialLoadOk ? 'check' : 'warning'} />
            <span>
              {scorecard.trialLoadOk
                ? evidenceCopy.validationPassed
                : evidenceCopy.validationIssues(
                    scorecard.constraintViolationCount,
                    scorecard.castFailureCount,
                  )}
            </span>
          </p>
        ) : null}
      </section>

      {allWarnings.length > 0 ? (
        <section className={styles.section}>
          <h3 className={styles.subtitle}>{evidenceCopy.warningsHeading}</h3>
          <ul className={styles.rawList}>
            {allWarnings.map((warning, idx) => (
              <li key={idx}>{warning}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
