import { useId } from 'react';
import { Label, Radio, RadioGroup } from 'react-aria-components';
import { Icon } from '../../components/index.ts';
import type { Purpose } from './api.ts';
import { purposeCopy } from './copy.ts';
import styles from './PurposeSelector.module.css';

export interface PurposeSelectorProps {
  value: Purpose;
  onChange: (value: Purpose) => void;
  compact?: boolean;
}

/**
 * Lets the user choose the DB design purpose (`일반 서비스용` / `통계·분석용` /
 * `원본 보관용`). When `원본 보관용` (`archive`) is selected, links the inline
 * guidance to the radio group and announces it politely via a persistent live region.
 */
export function PurposeSelector({ value, onChange, compact = false }: PurposeSelectorProps) {
  const archiveNoticeId = useId();
  const isArchive = value === 'archive';

  return (
    <div className={styles.wrapper}>
      <RadioGroup
        value={value}
        aria-describedby={isArchive ? archiveNoticeId : undefined}
        onChange={(next) => {
          if (next === 'oltp' || next === 'analytics' || next === 'archive') onChange(next);
        }}
        className={styles.group}
      >
        <Label className={styles.heading}>{purposeCopy.heading}</Label>
        <div className={`${styles.options} ${compact ? styles.compact : ''}`}>
          {purposeCopy.options.map((option) => (
            <Radio key={option.value} value={option.value} className={styles.option}>
              <span className={styles.indicator} aria-hidden="true" />
              <span className={styles.text}>
                <span className={styles.label}>{option.label}</span>
                <span className={styles.hint}>{option.hint}</span>
              </span>
            </Radio>
          ))}
        </div>
      </RadioGroup>

      <div role="status" className={styles.archiveStatus}>
        {isArchive ? (
          <p id={archiveNoticeId} className={styles.archiveNotice}>
            <Icon name="info" className={styles.archiveIcon} />
            <span>{purposeCopy.archiveNotice}</span>
          </p>
        ) : null}
      </div>
    </div>
  );
}
