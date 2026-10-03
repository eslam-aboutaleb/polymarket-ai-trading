/**
 * Authenticated shell page that renders the top navigation and the routed child outlet.
 *
 * The logout handler calls `POST /api/auth/logout` and clears the auth store in a `finally` block,
 * so local session state is dropped even if the server call fails.
 *
 * @module pages/DashboardPage
 */

import { Outlet } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import Navigation from "../components/Navigation";
import { authService } from "../services/authService";

export default function DashboardPage() {
  const { walletAddress, clearSession } = useAuthStore();

  const handleLogout = async () => {
    try {
      await authService.logout();
    } finally {
      clearSession();
    }
  };

  return (
    <div className="app-shell page-enter">
      <Navigation walletAddress={walletAddress || ""} onLogout={handleLogout} />

      <main className="max-w-7xl mx-auto px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
