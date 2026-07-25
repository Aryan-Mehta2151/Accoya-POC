import * as Dialog from '@radix-ui/react-dialog';
import { AlertCircle, LoaderCircle, X } from 'lucide-react';
import type { ReactNode } from 'react';
import styles from './ui.module.css';

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className={styles.pageHeader}>
      <div className={styles.headerCopy}>
        {eyebrow && <p className={styles.eyebrow}>{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className={styles.description}>{description}</p>}
      </div>
      {actions && <div className={styles.headerActions}>{actions}</div>}
    </header>
  );
}

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className={styles.state} role='status' aria-live='polite'>
      <LoaderCircle className={styles.spinner} aria-hidden='true' />
      <p>{label}</p>
    </div>
  );
}

export function ErrorState({
  title = 'Something went wrong',
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className={`${styles.state} ${styles.errorState}`} role='alert'>
      <AlertCircle aria-hidden='true' />
      <div>
        <h2>{title}</h2>
        <p>{message}</p>
      </div>
      {onRetry && (
        <button className='button buttonSecondary' type='button' onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className={styles.emptyState}>
      {icon && <div className={styles.emptyIcon}>{icon}</div>}
      <h2>{title}</h2>
      <p>{description}</p>
      {action && <div className={styles.emptyAction}>{action}</div>}
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = {
  draft: 'Draft',
  pending_review: 'Needs review',
  approved: 'Approved',
  sent: 'Marked sent',
  rejected: 'Rejected',
  generating: 'Generating',
  generation_issue: 'Generation issue',
  no_email: 'No email',
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`${styles.statusBadge} ${styles[`status_${status}`] ?? ''}`}>
      <span aria-hidden='true' />
      {STATUS_LABELS[status] ?? status.replaceAll('_', ' ')}
    </span>
  );
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  pending = false,
  variant = 'default',
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  pending?: boolean;
  variant?: 'default' | 'danger';
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.dialogOverlay} />
        <Dialog.Content className={styles.dialogContent}>
          <Dialog.Close className={styles.dialogClose} aria-label='Close dialog'>
            <X aria-hidden='true' />
          </Dialog.Close>
          <Dialog.Title>{title}</Dialog.Title>
          <Dialog.Description>{description}</Dialog.Description>
          <div className={styles.dialogActions}>
            <Dialog.Close className='button buttonGhost' disabled={pending}>
              Cancel
            </Dialog.Close>
            <button
              className={`button ${variant === 'danger' ? 'buttonDanger' : 'buttonPrimary'}`}
              type='button'
              disabled={pending}
              onClick={onConfirm}
            >
              {pending && <LoaderCircle className={styles.buttonSpinner} aria-hidden='true' />}
              {pending ? 'Working…' : confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function GenerationOverlay({
  open,
  title,
  description,
}: {
  open: boolean;
  title: string;
  description: string;
}) {
  if (!open) return null;
  return (
    <div className={styles.generationOverlay} role='status' aria-live='assertive'>
      <div className={styles.generationCard}>
        <div className={styles.generationMark} aria-hidden='true'>
          <span />
          <span />
          <span />
        </div>
        <p className={styles.eyebrow}>Accoya Outreach</p>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
    </div>
  );
}
