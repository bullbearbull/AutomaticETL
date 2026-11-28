import { useId } from 'react';
import { Checkbox, Text } from 'react-aria-components';
import { Details, Icon } from '../../components/index.ts';
import type { DbEngine, OrphanPolicy } from './api.ts';
import { settingsCopy } from './copy.ts';
import type { DatabaseSettings } from './state.ts';
import styles from './SettingsPanel.module.css';

export interface SettingsPanelProps {
  settings: DatabaseSettings;
  onChange: (patch: Partial<DatabaseSettings>) => void;
  isExpanded: boolean;
  onExpandedChange: (isExpanded: boolean) => void;
}

/**
 * Folded advanced settings for the DB tab (`세부 설정`). All defaults match
 * the backend engine defaults (`sqlite`, `quarantine`, `decomposeStrings=true`).
 */
export function SettingsPanel({ settings, onChange, isExpanded, onExpandedChange }: SettingsPanelProps) {
  const engineId = useId();
  const orphanId = useId();
  const notesId = useId();

  return (
    <Details
      summary={settingsCopy.heading}
      isExpanded={isExpanded}
      onExpandedChange={onExpandedChange}
      className={styles.details}
    >
      <div className={styles.panel}>
        <div className={styles.field}>
          <label htmlFor={engineId} className={styles.label}>
            {settingsCopy.dbEngine.label}
          </label>
          <select
            id={engineId}
            value={settings.dbEngine}
            onChange={(e) => onChange({ dbEngine: e.target.value as DbEngine })}
            className={styles.select}
          >
            {settingsCopy.dbEngine.options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.field}>
          <label htmlFor={orphanId} className={styles.label}>
            {settingsCopy.orphanPolicy.label}
          </label>
          <select
            id={orphanId}
            value={settings.orphanPolicy}
            onChange={(e) => onChange({ orphanPolicy: e.target.value as OrphanPolicy })}
            className={styles.select}
          >
            {settingsCopy.orphanPolicy.options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.checks}>
          <ToggleOption
            checked={settings.decomposeStrings}
            onChange={(val) => onChange({ decomposeStrings: val })}
            label={settingsCopy.decomposeStrings.label}
            description={settingsCopy.decomposeStrings.description}
          />
          <ToggleOption
            checked={settings.includeAuditColumns}
            onChange={(val) => onChange({ includeAuditColumns: val })}
            label={settingsCopy.includeAuditColumns.label}
            description={settingsCopy.includeAuditColumns.description}
          />
          <ToggleOption
            checked={settings.includeManagementTables}
            onChange={(val) => onChange({ includeManagementTables: val })}
            label={settingsCopy.includeManagementTables.label}
            description={settingsCopy.includeManagementTables.description}
          />
        </div>

        <div className={styles.field}>
          <label htmlFor={notesId} className={styles.label}>
            {settingsCopy.notes.label}
          </label>
          <input
            id={notesId}
            type="text"
            value={settings.notes}
            onChange={(e) => onChange({ notes: e.target.value })}
            placeholder={settingsCopy.notes.placeholder}
            className={styles.input}
          />
          <span className={styles.description}>{settingsCopy.notes.description}</span>
        </div>
      </div>
    </Details>
  );
}

function ToggleOption({
  checked,
  onChange,
  label,
  description,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description: string;
}) {
  return (
    <div className={styles.toggleField}>
      <Checkbox isSelected={checked} onChange={onChange} className={styles.toggle}>
        {({ isSelected }) => (
          <>
            <span className={styles.box} aria-hidden="true">
              {isSelected ? <Icon name="check" /> : null}
            </span>
            <span className={styles.toggleLabel}>{label}</span>
          </>
        )}
      </Checkbox>
      <Text slot="description" className={styles.toggleDescription}>
        {description}
      </Text>
    </div>
  );
}
