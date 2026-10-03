/**
 * Stop-loss trigger modal.
 *
 * Thin wrapper around {@link PriceTriggerModal} that supplies the stop-loss
 * direction (triggered by a price fall) and the stop-loss endpoints. All
 * shared layout, validation and P&L presentation lives in the shared component.
 * Setting a stop-loss is an auto-trading action, so it requires a stored
 * trading key.
 */

import PriceTriggerModal, {
  PriceTriggerModalProps,
  TriggerConfig,
  TriggerPosition,
} from "./PriceTriggerModal";
import { stopLossService, StopLossOrder } from "../services/stopLossService";

export type StopLossModalProps = Omit<
  PriceTriggerModalProps,
  "config" | "existingOrder" | "existingPrice"
> & {
  position: TriggerPosition;
  /** Existing stop-loss for this position (if any). */
  existingStopLoss?: StopLossOrder | null;
};

const STOP_LOSS_CONFIG: TriggerConfig = {
  label: "Stop Loss",
  priceField: "stop_price",
  direction: -1,
  // 10¢ below the current price, clamped to 1-99¢.
  defaultOffsetCents: -10,
  minCents: 1,
  maxCents: 99,
  service: {
    setTrigger: (request) =>
      stopLossService.setStopLoss(
        request as unknown as Parameters<typeof stopLossService.setStopLoss>[0],
      ),
    cancelTrigger: (orderId) => stopLossService.cancelStopLoss(orderId),
  },
  accentClass: "accent-[var(--danger)] focus:border-[var(--danger)]",
  statusClass: "status-bad",
  actionClass: "btn-danger",
  calloutClass: "bg-[var(--accent-soft)] border-[var(--accent)]/30 text-[var(--accent-strong)]",
  calloutIcon: "⏱",
  howItWorks:
    "Prices are monitored every 10 seconds. When the price drops to or below your stop price, a sell order is automatically placed.",
  impactLabel: "Max Loss if Triggered",
};

export default function StopLossModal({
  position,
  existingStopLoss,
  onClose,
  onSuccess,
}: StopLossModalProps) {
  return (
    <PriceTriggerModal
      position={position}
      existingOrder={existingStopLoss ?? undefined}
      existingPrice={existingStopLoss?.stop_price ?? null}
      config={STOP_LOSS_CONFIG}
      requireTradingKey
      onClose={onClose}
      onSuccess={onSuccess}
    />
  );
}
