/**
 * Confirmation dialog wrapping the shared `Modal` primitive.
 *
 * Thin composition layer for confirm-destructive flows: it renders the
 * cancel/confirm row through `Modal`'s `footer` prop and stacks above any
 * already-open modal via `zLevel="modalNested"`. Escape, backdrop click, and
 * the panel close button all route through `onCancel`; the confirm button uses
 * `btn-danger` when `destructive`, else `btn-success`.
 *
 * @module components/ui/ConfirmModal
 */

import Modal from "./Modal";

export interface ConfirmModalProps {
  open: boolean;
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  confirmDisabled?: boolean;
}

export default function ConfirmModal({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  onConfirm,
  onCancel,
  confirmDisabled = false,
}: ConfirmModalProps) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      zLevel="modalNested"
      footer={
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="btn-muted text-sm">
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={confirmDisabled}
            className={`text-sm ${destructive ? "btn-danger" : "btn-success"}`}
          >
            {confirmLabel}
          </button>
        </div>
      }
    >
      {body}
    </Modal>
  );
}
