/**
 * Take-profit trigger modal.
 *
 * Thin wrapper around {@link PriceTriggerModal} that supplies the take-profit
 * direction (triggered by a price rise) and the take-profit endpoints. All
 * shared layout, validation and P&L presentation lives in the shared component.
 */

import PriceTriggerModal, {
  PriceTriggerModalProps,
  TriggerConfig,
  TriggerPosition,
} from "./PriceTriggerModal";
import { takeProfitService, TakeProfitOrder } from "../services/takeProfitService";

export type TakeProfitModalProps = Omit<
  PriceTriggerModalProps,
  "config" | "existingOrder" | "existingPrice"
> & {
  position: TriggerPosition;
  /** Existing take-profit for this position (if any). */
  existingTakeProfit?: TakeProfitOrder | null;
};

const TAKE_PROFIT_CONFIG: TriggerConfig = {
  label: "Take Profit",
  priceField: "take_profit_price",
  direction: 1,
  // 10¢ above the current price, clamped to 2-99¢.
  defaultOffsetCents: 10,
  minCents: 2,
  maxCents: 99,
  service: {
    setTrigger: (request) =>
      takeProfitService.setTakeProfit(
        request as unknown as Parameters<typeof takeProfitService.setTakeProfit>[0],
      ),
    cancelTrigger: (orderId) => takeProfitService.cancelTakeProfit(orderId),
  },
  accentClass: "accent-emerald-500 focus:border-emerald-500",
  statusClass: "status-good",
  actionClass: "btn-primary",
  calloutClass: "bg-emerald-500/10 border-emerald-500/30 text-emerald-400",
  calloutIcon: "📈",
  howItWorks:
    "Prices are monitored every 10 seconds. When the price rises to or above your target price, a sell order is automatically placed to lock in your profits.",
  impactLabel: "Estimated Profit if Triggered",
};

export default function TakeProfitModal({
  position,
  existingTakeProfit,
  onClose,
  onSuccess,
}: TakeProfitModalProps) {
  return (
    <PriceTriggerModal
      position={position}
      existingOrder={existingTakeProfit ?? undefined}
      existingPrice={existingTakeProfit?.take_profit_price ?? null}
      config={TAKE_PROFIT_CONFIG}
      onClose={onClose}
      onSuccess={onSuccess}
    />
  );
}
