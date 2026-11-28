import { useId } from 'react';
import { Details } from '../../components/index.ts';
import type { RerankerKind } from './api.ts';
import { settingsCopy } from './copy.ts';
import type { SearchSettings } from './state.ts';
import styles from './SettingsPanel.module.css';

export interface SettingsPanelProps {
  settings: SearchSettings;
  onChange: (patch: Partial<SearchSettings>) => void;
  isExpanded: boolean;
  onExpandedChange: (isExpanded: boolean) => void;
  disabled?: boolean;
}

/**
 * Folded advanced search settings (`세부 설정`): result count, same-meaning
 * synonyms, and optional local ONNX reranker.
 */
export function SettingsPanel({
  settings,
  onChange,
  isExpanded,
  onExpandedChange,
  disabled = false,
}: SettingsPanelProps) {
  const topKId = useId();
  const topKDescId = useId();
  const synonymsId = useId();
  const synonymsDescId = useId();
  const rerankerId = useId();
  const modelId = useId();
  const modelDescId = useId();

  return (
    <Details
      summary={settingsCopy.heading}
      isExpanded={isExpanded}
      onExpandedChange={onExpandedChange}
      className={styles.details}
    >
      <div className={styles.panel}>
        <div className={styles.field}>
          <label htmlFor={topKId} className={styles.label}>
            {settingsCopy.topK.label}
          </label>
          <input
            id={topKId}
            type="number"
            min={1}
            max={20}
            value={settings.topK}
            disabled={disabled}
            aria-describedby={topKDescId}
            onChange={(e) => {
              const raw = Number(e.target.value);
              const clamped = Number.isFinite(raw) ? Math.max(1, Math.min(20, Math.trunc(raw))) : 5;
              onChange({ topK: clamped });
            }}
            className={styles.input}
          />
          <span id={topKDescId} className={styles.description}>
            {settingsCopy.topK.description}
          </span>
        </div>

        <div className={styles.field}>
          <label htmlFor={synonymsId} className={styles.label}>
            {settingsCopy.synonyms.label}
          </label>
          <input
            id={synonymsId}
            type="text"
            value={settings.synonyms}
            disabled={disabled}
            aria-describedby={synonymsDescId}
            placeholder={settingsCopy.synonyms.placeholder}
            onChange={(e) => onChange({ synonyms: e.target.value })}
            className={styles.input}
          />
          <span id={synonymsDescId} className={styles.description}>
            {settingsCopy.synonyms.description}
          </span>
        </div>

        <div className={styles.field}>
          <label htmlFor={rerankerId} className={styles.label}>
            {settingsCopy.reranker.label}
          </label>
          <select
            id={rerankerId}
            value={settings.reranker}
            disabled={disabled}
            onChange={(e) => onChange({ reranker: e.target.value as RerankerKind })}
            className={styles.select}
          >
            {settingsCopy.reranker.options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        {settings.reranker === 'onnx' ? (
          <div className={styles.field}>
            <label htmlFor={modelId} className={styles.label}>
              {settingsCopy.rerankerModel.label}
            </label>
            <input
              id={modelId}
              type="text"
              value={settings.rerankerModel}
              disabled={disabled}
              aria-describedby={modelDescId}
              placeholder={settingsCopy.rerankerModel.placeholder}
              onChange={(e) => onChange({ rerankerModel: e.target.value })}
              className={styles.input}
            />
            <span id={modelDescId} className={styles.description}>
              {settingsCopy.rerankerModel.description}
            </span>
          </div>
        ) : null}
      </div>
    </Details>
  );
}
