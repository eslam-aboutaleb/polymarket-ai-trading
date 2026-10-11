/**
 * Standalone risk-report page: concentration, loss bound, correlated clusters, catalysts.
 *
 * Renders `PortfolioRiskPanel`, which owns its own fetch. Keeping this on its own route (rather
 * than inside the Dashboard) means the risk surface can load and fail independently of the
 * portfolio summary.
 *
 * @module pages/RiskReportPage
 */

import PortfolioRiskPanel from "../components/PortfolioRiskPanel";

export default function RiskReportPage() {
  return (
    <div className="max-w-6xl mx-auto">
      <PortfolioRiskPanel />
    </div>
  );
}
