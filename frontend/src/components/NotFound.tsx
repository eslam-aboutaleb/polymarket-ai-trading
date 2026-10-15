/**
 * 404 page rendered by the catch-all route when a URL matches no known page.
 *
 * Replaces the previous silent redirect to Markets, where a mistyped or stale
 * bookmark gave no signal that the URL was wrong. Offers a public Markets
 * link, a Dashboard link for authenticated sessions, and the Guide.
 *
 * @module components/NotFound
 */
import { Link } from "react-router-dom";
import { useAuthStore } from "../store/authStore";

export default function NotFound() {
  const { isAuthenticated } = useAuthStore();

  return (
    <div className="app-shell min-h-screen flex items-center justify-center px-4 py-12">
      <div className="surface-panel w-full max-w-lg p-8 text-center page-enter" role="status">
        <p className="mono text-sm text-muted mb-2">404</p>
        <h1 className="text-3xl font-bold mb-3">Page not found</h1>
        <p className="text-soft text-sm leading-relaxed">
          The address you opened does not match any page in this app, usually because of a typo or a
          link that has moved. Nothing was changed or removed — your sessions, positions and trades
          are untouched. Pick a destination below to keep going.
        </p>
        <div className="flex flex-wrap items-center justify-center gap-3 mt-6">
          <Link to="/markets" className="btn-accent inline-flex items-center justify-center gap-2">
            Browse markets
          </Link>
          {isAuthenticated && (
            <Link to="/" className="btn-accent inline-flex items-center justify-center gap-2">
              Go to dashboard
            </Link>
          )}
          <Link to="/guide" className="btn-muted inline-flex items-center justify-center gap-2">
            Read the guide
          </Link>
        </div>
      </div>
    </div>
  );
}
