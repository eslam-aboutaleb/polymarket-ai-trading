import { sumCostLines, type CostLine } from "../../lib/evidence";

const fmt = (n: number) => `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`;

/** Cost waterfall. Unknown lines show "unknown" and the total says it is partial. */
export default function CostBreakdown({ lines, caption }: { lines: CostLine[]; caption?: string }) {
  const { total, hasUnknown } = sumCostLines(lines);
  return (
    <table className="w-full text-xs" data-testid="cost-breakdown">
      <caption className="mb-1 text-left text-[var(--pm-text-soft)]">
        {caption ?? "Cost breakdown (USDC)"}
      </caption>
      <tbody>
        {lines.map((l) => (
          <tr key={l.label}>
            <th scope="row" className="py-0.5 text-left font-normal">
              {l.label}
              <span className="ml-1 text-[var(--pm-text-soft)]">
                {l.measured ? "(measured)" : "(modeled)"}
              </span>
            </th>
            <td className="py-0.5 text-right mono">
              {l.amount == null ? "unknown" : fmt(l.amount)}
            </td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row" className="pt-1 text-left font-medium">
            {hasUnknown ? "Total of known lines" : "Total"}
          </th>
          <td className="pt-1 text-right mono font-medium">{fmt(total)}</td>
        </tr>
      </tfoot>
    </table>
  );
}
