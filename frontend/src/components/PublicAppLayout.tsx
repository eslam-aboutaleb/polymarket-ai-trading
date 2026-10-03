/**
 * Router layout for public pages: navigation, the routed outlet and the login modal.
 *
 * Passes isPublic={!isAuthenticated} to Navigation so visitors see the Connect Wallet CTA, and
 * binds a global "/" shortcut that dispatches FOCUS_MARKET_SEARCH_EVENT outside form fields.
 *
 * @module components/PublicAppLayout
 */
import { useEffect } from "react";
import { Outlet } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { authService } from "../services/authService";
import Navigation from "./Navigation";
import LoginModal from "./LoginModal";
import { FOCUS_MARKET_SEARCH_EVENT } from "../utils/tradingWorkspace";

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return !!el.closest("input, textarea, select, [contenteditable='true']");
}

/**
 * Layout for public pages (Markets, Opportunities, Leaderboard, Guide, Analysis).
 * Renders navigation with public nav items + "Connect Wallet" CTA for visitors.
 * If the user is authenticated, shows the full navigation (same as AppLayout).
 */
export default function PublicAppLayout() {
  const { walletAddress, isAuthenticated, clearSession } = useAuthStore();

  const handleLogout = async () => {
    try {
      await authService.logout();
    } finally {
      clearSession();
    }
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key !== "/") return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isEditableTarget(event.target)) return;
      event.preventDefault();
      window.dispatchEvent(new CustomEvent(FOCUS_MARKET_SEARCH_EVENT));
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="app-shell page-enter">
      <Navigation
        walletAddress={walletAddress || ""}
        onLogout={handleLogout}
        isPublic={!isAuthenticated}
      />
      <main className="relative z-0 max-w-7xl mx-auto px-4 py-8">
        <Outlet />
      </main>
      <LoginModal />
    </div>
  );
}
