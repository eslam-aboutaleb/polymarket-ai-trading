import { Link } from "react-router-dom";
import { experimentHref } from "../../lib/evidence";

export interface VersionedExperimentLinkProps {
  experimentId?: string | null;
  datasetVersion?: string | null;
  feeVersion?: string | null;
  modelVersion?: string | null;
}

/** Link to the reproducible report. Renders plain text when any version is missing. */
export default function VersionedExperimentLink(props: VersionedExperimentLinkProps) {
  const href = experimentHref(props);
  if (!href) {
    return (
      <span className="text-xs text-[var(--pm-text-soft)]" data-testid="experiment-link-missing">
        No reproducible report (dataset, fee or experiment version missing)
      </span>
    );
  }
  return (
    <Link to={href} className="text-xs underline" data-testid="experiment-link">
      Reproducible report: {props.experimentId} (dataset {props.datasetVersion}, fees{" "}
      {props.feeVersion}
      {props.modelVersion ? `, model ${props.modelVersion}` : ""})
    </Link>
  );
}
