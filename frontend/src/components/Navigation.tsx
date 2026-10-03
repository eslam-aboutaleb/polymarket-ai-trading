/**
 * Top navigation bar: brand button, hamburger dropdown, and the wallet/logout or connect CTA.
 *
 * Visible items come from navItems, restricted to PUBLIC_NAV_IDS when isPublic is set and to
 * non-debug routes for non-admins; the highlighted entry is derived from the router pathname.
 *
 * @module components/Navigation
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { useLoginModal } from "../context/LoginModalContext";
import NotificationCenter from "./NotificationCenter";

type PageId =
  | "dashboard"
  | "markets"
  | "opportunities"
  | "leaderboard"
  | "copy-trading"
  | "market-making"
  | "backtesting"
  | "news"
  | "guide"
  | "trades"
  | "whales"
  | "latency-arb"
  | "ops"
  | "settings"
  | "debug";

interface NavigationProps {
  walletAddress: string;
  onLogout: () => void | Promise<void>;
  /** When true, show only public nav items and a Connect Wallet CTA */
  isPublic?: boolean;
}

const navItems: { id: PageId; label: string; path: string }[] = [
  { id: "dashboard", label: "Dashboard", path: "/" },
  { id: "markets", label: "Markets", path: "/markets" },
  { id: "opportunities", label: "Opportunities", path: "/opportunities" },
  { id: "leaderboard", label: "Leaderboard", path: "/leaderboard" },
  { id: "copy-trading", label: "Copy Trading", path: "/copy-trading" },
  { id: "market-making", label: "Market Making", path: "/market-making" },
  { id: "backtesting", label: "Backtesting", path: "/backtesting" },
  { id: "news", label: "📰 News", path: "/news" },
  { id: "guide", label: "\u{1F4D6} Your Guide", path: "/guide" },
  { id: "trades", label: "Trades", path: "/trades" },
  { id: "whales", label: "🐋 Whales", path: "/whales" },
  { id: "latency-arb", label: "⚡ Latency Arb", path: "/latency-arb" },
  { id: "ops", label: "Ops Center", path: "/ops" },
  { id: "settings", label: "Settings", path: "/settings" },
  { id: "debug", label: "\u{1F527} Debug", path: "/debug" },
];

/** Nav items visible to unauthenticated visitors */
const PUBLIC_NAV_IDS = new Set<PageId>([
  "markets",
  "opportunities",
  "leaderboard",
  "copy-trading",
  "guide",
]);

/** Map a pathname to the matching nav id */
function activeId(pathname: string): PageId {
  // /analysis tab should highlight "opportunities"
  if (pathname.startsWith("/analysis")) return "opportunities";
  if (pathname.startsWith("/debug")) return "debug";
  const match = navItems.find((n) => n.path !== "/" && pathname.startsWith(n.path));
  if (match) return match.id;
  return "dashboard";
}

export default function Navigation({ walletAddress, onLogout, isPublic = false }: NavigationProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const [logoutLoading, setLogoutLoading] = useState(false);
  const menuContainerRef = useRef<HTMLDivElement | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const { isAdmin } = useAuthStore();
  const { openLoginModal } = useLoginModal();
  const currentPage = activeId(location.pathname);

  const visibleNavItems = isPublic
    ? navItems.filter((n) => PUBLIC_NAV_IDS.has(n.id))
    : isAdmin
      ? navItems
      : navItems.filter((n) => n.id !== "debug");

  const shortAddress = walletAddress
    ? `${walletAddress.slice(0, 6)}...${walletAddress.slice(-4)}`
    : "";

  useEffect(() => {
    setShowMenu(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!showMenu) return;

    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      if (!menuContainerRef.current) return;
      if (menuContainerRef.current.contains(event.target as Node)) return;
      setShowMenu(false);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowMenu(false);
      }
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [showMenu]);

  const handleLogoutConfirm = async () => {
    try {
      setLogoutLoading(true);
      await onLogout();
      setShowLogoutConfirm(false);
    } finally {
      setLogoutLoading(false);
    }
  };

  return (
    <>
      <nav className="relative isolate z-40 border-b border-[var(--line)] bg-[#121722]/90 backdrop-blur">
        <div className="max-w-7xl mx-auto px-4">
          <div className="flex items-center justify-between h-16">
            {/* Logo */}
            <div ref={menuContainerRef} className="relative flex items-center gap-2">
              <button
                type="button"
                onClick={() => setShowMenu((prev) => !prev)}
                className="p-2 rounded-md text-soft hover:bg-[var(--bg-soft)] transition"
                aria-label="Open navigation menu"
                aria-expanded={showMenu}
                aria-controls="primary-nav-menu"
                aria-haspopup="menu"
              >
                <svg
                  className="w-5 h-5"
                  viewBox="0 0 24 24"
                  fill="none"
                  xmlns="http://www.w3.org/2000/svg"
                >
                  <path
                    d="M4 7H20M4 12H20M4 17H20"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
              <button
                type="button"
                onClick={() => {
                  navigate(isPublic ? "/markets" : "/");
                  setShowMenu(false);
                }}
                className="text-xl font-extrabold theme-brand hover:opacity-90 transition"
                aria-label="Go to dashboard"
              >
                POLYMARKET AI
              </button>

              {showMenu && (
                <div
                  id="primary-nav-menu"
                  role="menu"
                  aria-label="Primary navigation"
                  className="absolute left-0 top-full mt-2 z-[60] min-w-[220px] p-2 rounded-xl border border-[var(--line)] bg-[var(--bg-panel)] shadow-[var(--shadow)]"
                >
                  {visibleNavItems.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        navigate(item.path);
                        setShowMenu(false);
                      }}
                      className={`block w-full text-left px-3 py-2 rounded-md text-sm font-semibold transition ${
                        currentPage === item.id
                          ? "bg-[var(--accent-soft)] text-[var(--accent)] border border-[#f0b74155]"
                          : "text-soft hover:bg-[var(--bg-soft)]"
                      }`}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Right Section */}
            <div className="flex items-center space-x-4">
              {isPublic ? (
                <button
                  onClick={openLoginModal}
                  className="btn-accent font-semibold text-sm px-4 py-2"
                >
                  Connect Wallet
                </button>
              ) : (
                <>
                  <NotificationCenter />

                  <div className="hidden sm:flex items-center px-3 py-2 rounded-md text-sm text-soft border border-[var(--line)] bg-[var(--bg-elevated)] mono">
                    {shortAddress}
                  </div>

                  {/* Logout Button */}
                  <button onClick={() => setShowLogoutConfirm(true)} className="btn-danger">
                    Logout
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </nav>

      {showLogoutConfirm && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center px-4">
          <div
            className="absolute inset-0 modal-overlay"
            onClick={() => (logoutLoading ? null : setShowLogoutConfirm(false))}
          />
          <div className="relative z-[81] w-full max-w-sm surface-panel p-5 space-y-4">
            <h3 className="text-base font-semibold text-white">Confirm Logout</h3>
            <p className="text-sm text-soft">Are you sure you want to end your session now?</p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowLogoutConfirm(false)}
                disabled={logoutLoading}
                className="btn-muted text-sm"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleLogoutConfirm}
                disabled={logoutLoading}
                className="btn-danger text-sm"
              >
                {logoutLoading ? "Logging out..." : "Logout"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
