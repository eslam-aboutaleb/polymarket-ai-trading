import { Outlet } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { authService } from "../services/authService";
import Navigation from "./Navigation";

export default function AppLayout() {
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
      <main className="relative z-0 max-w-7xl mx-auto px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
