import type { ReactNode } from 'react';
import { Dialog, Heading, Modal, ModalOverlay } from 'react-aria-components';
import { buttonCopy } from '../../copy/common.ts';
import { Button } from '../Button/Button.tsx';
import styles from './ConfirmDialog.module.css';

export interface ConfirmAction {
  /** What the button does, as "무엇을 + 하기" ("가리고 저장하기"). Never 예/아니오. */
  label: string;
  onAction: () => void;
  /** Exactly one action should be primary; it gets the focus when the dialog opens. */
  primary?: boolean;
}

export interface ConfirmDialogProps {
  isOpen: boolean;
  /** Called with false on 취소, Esc, or after an action runs. */
  onOpenChange: (isOpen: boolean) => void;
  /** A question or a fact: "개인정보로 보이는 열 2개가 아직 가려지지 않았어요". */
  title: string;
  /** What happens with each choice. */
  children: ReactNode;
  /** The action buttons, primary first. 취소 is added at the end. */
  actions: readonly ConfirmAction[];
  /** Text of the cancel button. Defaults to "취소". */
  cancelLabel?: string;
}

/**
 * A modal confirmation (React Aria Dialog + Modal, `role="alertdialog"`).
 * Focus starts on the primary action and stays inside until it closes;
 * Esc equals 취소. Clicking the backdrop does not close it.
 */
export function ConfirmDialog({
  isOpen,
  onOpenChange,
  title,
  children,
  actions,
  cancelLabel = buttonCopy.cancel,
}: ConfirmDialogProps) {
  return (
    <ModalOverlay isOpen={isOpen} onOpenChange={onOpenChange} isDismissable={false} className={styles.overlay}>
      <Modal className={styles.modal}>
        <Dialog role="alertdialog" className={styles.dialog}>
          {({ close }) => (
            <>
              <Heading slot="title" className={styles.title}>
                {title}
              </Heading>
              <div className={styles.body}>{children}</div>
              <div className={styles.actions}>
                {actions.map((action) => (
                  <Button
                    key={action.label}
                    variant={action.primary ? 'primary' : 'secondary'}
                    autoFocus={action.primary === true}
                    onPress={() => {
                      action.onAction();
                      close();
                    }}
                  >
                    {action.label}
                  </Button>
                ))}
                <Button onPress={close}>{cancelLabel}</Button>
              </div>
            </>
          )}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
