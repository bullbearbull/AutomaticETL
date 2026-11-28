import { useState, type ReactNode } from 'react';
import {
  Checkbox,
  CheckboxButton,
  CheckboxField,
  FieldError,
  Input,
  Label,
  Text,
  TextField,
  type CheckboxRenderProps,
} from 'react-aria-components';
import { Details, Icon } from '../../components/index.ts';
import { settingsCopy } from './copy.ts';
import {
  CHUNK_MAX_CHARS,
  CHUNK_OVERLAP,
  changedSettingsCount,
  normalizeCount,
  parsePageSpec,
  type CountRange,
  type ExtractSettings,
} from './settings.ts';
import styles from './SettingsPanel.module.css';

export interface SettingsPanelProps {
  settings: ExtractSettings;
  onChange: (settings: ExtractSettings) => void;
  /** Open state, kept by the tab so it survives the move from the start screen to the workspace. */
  isExpanded: boolean;
  onExpandedChange: (isExpanded: boolean) => void;
}

/**
 * 세부 설정: the seven reading options, folded by default. A change applies
 * to files read from now on; the tab offers [다시 읽기] for a file already read.
 */
export function SettingsPanel({ settings, onChange, isExpanded, onExpandedChange }: SettingsPanelProps) {
  // Show a page-list problem once the user leaves the field, not while typing.
  const [showPagesError, setShowPagesError] = useState(false);
  const set = <K extends keyof ExtractSettings>(key: K, value: ExtractSettings[K]) =>
    onChange({ ...settings, [key]: value });
  const pages = parsePageSpec(settings.pages);
  const pagesError = showPagesError && !pages.ok ? settingsCopy.pagesInvalid(pages.token) : null;
  const changed = changedSettingsCount(settings);

  const settleCount = (key: 'chunkMaxChars' | 'chunkOverlap', range: CountRange) => {
    const settled = String(normalizeCount(settings[key], range));
    if (settled !== settings[key]) set(key, settled);
  };

  return (
    <Details
      summary={changed > 0 ? settingsCopy.summaryChanged(changed) : settingsCopy.summary}
      isExpanded={isExpanded}
      onExpandedChange={onExpandedChange}
      className={styles.details}
    >
      <div className={styles.panel}>
        <div className={styles.checks}>
          <Toggle
            label={settingsCopy.commentsLabel}
            isSelected={settings.includeComments}
            onChange={(value) => set('includeComments', value)}
          />
          <Toggle
            label={settingsCopy.cropsLabel}
            isSelected={settings.extractCrops}
            onChange={(value) => set('extractCrops', value)}
          />
          <Toggle
            label={settingsCopy.chunksLabel}
            isSelected={settings.includeChunks}
            onChange={(value) => set('includeChunks', value)}
          />
          <Toggle
            label={settingsCopy.layoutLabel}
            description={settingsCopy.layoutDescription}
            isSelected={settings.findLayout}
            onChange={(value) => set('findLayout', value)}
          />
        </div>

        <TextField
          value={settings.pages}
          onChange={(value) => {
            setShowPagesError(false);
            set('pages', value);
          }}
          onBlur={() => setShowPagesError(true)}
          isInvalid={pagesError !== null}
          validationBehavior="aria"
          className={styles.field}
        >
          <Label className={styles.label}>{settingsCopy.pagesLabel}</Label>
          <Input className={styles.input} autoComplete="off" spellCheck={false} />
          <Text slot="description" className={styles.description}>
            {settingsCopy.pagesDescription}
          </Text>
          <FieldError className={styles.error}>
            <Icon name="error" className={styles.errorIcon} />
            {pagesError}
          </FieldError>
        </TextField>

        <div className={styles.numbers}>
          <NumberField
            label={settingsCopy.chunkMaxLabel}
            description={settingsCopy.chunkMaxDescription}
            value={settings.chunkMaxChars}
            isDisabled={!settings.includeChunks}
            onChange={(value) => set('chunkMaxChars', value)}
            onBlur={() => settleCount('chunkMaxChars', CHUNK_MAX_CHARS)}
          />
          <NumberField
            label={settingsCopy.chunkOverlapLabel}
            description={settingsCopy.chunkOverlapDescription}
            value={settings.chunkOverlap}
            isDisabled={!settings.includeChunks}
            onChange={(value) => set('chunkOverlap', value)}
            onBlur={() => settleCount('chunkOverlap', CHUNK_OVERLAP)}
          />
        </div>
        <p className={styles.note}>{settingsCopy.numberNote}</p>
      </div>
    </Details>
  );
}

function ToggleFace({ isSelected, label }: { isSelected: boolean; label: ReactNode }) {
  return (
    <>
      <span className={styles.box} aria-hidden="true">
        {isSelected ? <Icon name="check" /> : null}
      </span>
      <span className={styles.toggleLabel}>{label}</span>
    </>
  );
}

function Toggle({
  label,
  description,
  isSelected,
  onChange,
}: {
  label: string;
  description?: string;
  isSelected: boolean;
  onChange: (value: boolean) => void;
}) {
  const face = ({ isSelected: selected }: Pick<CheckboxRenderProps, 'isSelected'>) => (
    <ToggleFace isSelected={selected} label={label} />
  );
  if (!description) {
    return (
      <Checkbox isSelected={isSelected} onChange={onChange} className={styles.toggle}>
        {face}
      </Checkbox>
    );
  }
  return (
    <CheckboxField isSelected={isSelected} onChange={onChange} className={styles.toggleField}>
      <CheckboxButton className={styles.toggle}>{face}</CheckboxButton>
      <Text slot="description" className={styles.toggleDescription}>
        {description}
      </Text>
    </CheckboxField>
  );
}

function NumberField({
  label,
  description,
  value,
  isDisabled,
  onChange,
  onBlur,
}: {
  label: string;
  description: string;
  value: string;
  isDisabled: boolean;
  onChange: (value: string) => void;
  onBlur: () => void;
}) {
  return (
    <TextField
      value={value}
      onChange={onChange}
      onBlur={onBlur}
      isDisabled={isDisabled}
      validationBehavior="aria"
      className={styles.field}
    >
      <Label className={styles.label}>{label}</Label>
      <Input className={`${styles.input} ${styles.numberInput} tabular-nums`} inputMode="numeric" autoComplete="off" />
      <Text slot="description" className={styles.description}>
        {description}
      </Text>
    </TextField>
  );
}
