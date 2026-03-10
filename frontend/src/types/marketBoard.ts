import { TradeTicketOutcome, TradeTicketSource } from "./trading";

export type MarketBoardVariant =
  | "binary_single"
  | "multi_option"
  | "price_direction";

export interface MarketBoardAiSummary {
  score: number;
  recommendation: string;
  risk: string;
}

export interface MarketBoardRowVM {
  id: string;
  label: string;
  probabilityText?: string;
  yesPriceText?: string;
  noPriceText?: string;
  yesLabel?: string;
  noLabel?: string;
}

export interface MarketBoardCardVM {
  id: string;
  source: TradeTicketSource;
  variant: MarketBoardVariant;
  title: string;
  image?: string;
  href?: string | null;
  probability?: number;
  probabilityLabel?: string;
  rows?: MarketBoardRowVM[];
  positiveLabel?: string;
  negativeLabel?: string;
  positiveOutcome?: TradeTicketOutcome;
  negativeOutcome?: TradeTicketOutcome;
  positiveMeta?: string;
  negativeMeta?: string;
  footerMeta: string;
  footerSubMeta?: string;
  isLive?: boolean;
  isFavorite: boolean;
  aiSummary?: MarketBoardAiSummary;
}
