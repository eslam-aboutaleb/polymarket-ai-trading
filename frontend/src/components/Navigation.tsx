/**
 * Top navigation bar: brand button, hamburger dropdown, and the wallet/logout or connect CTA.
 *
 * Visible items come from navItems, restricted to PUBLIC_NAV_IDS when isPublic is set and to
 * non-debug routes for non-admins; the highlighted entry is derived from the router pathname.
 * Authenticated visitors also get an account switcher: it persists the chosen account in
 * `localStorage` so every subsequent `accountService` call sends `X-Account-Id`.
 *
 * @module components/Navigation
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { useLoginModal } from "../context/LoginModalContext";
import NotificationCenter from "./NotificationCenter";
import { ConfirmModal } from "./ui";
import { resolveZ } from "../lib/zIndex";
import {
  Account,
  accountService,
  getActiveAccountSelection,
  onActiveAccountChange,
  setActiveAccount,
} from "../services/accountService";

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
  | "venues"
  | "calibration"
  | "orchestrator"
  | "command-palette"
  | "analytics-risk"
  | "execution-analytics"
  | "analysis"
  | "ops"
  | "settings"
  | "debug";

interface NavigationProps {
  walletAddress: string;
  onLogout: () => void | Promise<void>;
  /** When true, show only public nav items and a Connect Wallet CTA */
  isPublic?: boolean;
}

interface NavItem {
  id: PageId;
  label: string;
  path: string;
}

interface NavGroup {
  group: string;
  items: NavItem[];
}

const navGroups: NavGroup[] = [
  {
    group: "Trade",
    items: [
      { id: "dashboard", label: "Dashboard", path: "/" },
      { id: "markets", label: "Markets", path: "/markets" },
      { id: "opportunities", label: "Opportunities", path: "/opportunities" },
      { id: "trades", label: "Trades", path: "/trades" },
    ],
  },
  {
    group: "Discover",
    items: [
      { id: "leaderboard", label: "Leaderboard", path: "/leaderboard" },
      { id: "copy-trading", label: "Copy Trading", path: "/copy-trading" },
      { id: "whales", label: "Whales", path: "/whales" },
      { id: "news", label: "News", path: "/news" },
      { id: "venues", label: "Venues", path: "/venues" },
    ],
  },
  {
    group: "Automate",
    items: [
      { id: "market-making", label: "Market Making", path: "/market-making" },
      { id: "orchestrator", label: "Orchestrator", path: "/orchestrator" },
      { id: "latency-arb", label: "Latency Arb", path: "/latency-arb" },
      { id: "backtesting", label: "Backtesting", path: "/backtesting" },
    ],
  },
  {
    group: "Analyze",
    items: [
      { id: "calibration", label: "AI Calibration", path: "/analytics/calibration" },
      { id: "analytics-risk", label: "Risk Report", path: "/analytics/risk" },
      { id: "execution-analytics", label: "Execution Analytics", path: "/analytics/execution" },
      { id: "analysis", label: "Analysis", path: "/analysis" },
      { id: "command-palette", label: "Ask the AI", path: "/command-palette" },
    ],
  },
  {
    group: "Ops",
    items: [
      { id: "ops", label: "Ops Center", path: "/ops" },
      { id: "settings", label: "Settings", path: "/settings" },
      { id: "guide", label: "\u{1F4D6} Your Guide", path: "/guide" },
      { id: "debug", label: "Debug", path: "/debug" },
    ],
  },
];

const navItems: NavItem[] = navGroups.flatMap((group) => group.items);

/** Nav items visible to unauthenticated visitors */
const PUBLIC_NAV_IDS = new Set<PageId>([
  "markets",
  "opportunities",
  "leaderboard",
  "copy-trading",
  "guide",
]);

const ACTIVE_ID_ROUTES: { prefix: string; id: PageId }[] = [
  { prefix: "/analytics/execution", id: "execution-analytics" },
  { prefix: "/analytics/risk", id: "analytics-risk" },
  { prefix: "/analytics/calibration", id: "calibration" },
  { prefix: "/analysis", id: "analysis" },
  { prefix: "/debug", id: "debug" },
];

const navItemBaseClass = "flex items-center rounded-md text-sm font-semibold transition";
const navActiveClass = "bg-[var(--accent-soft)] text-[var(--accent)] border border-[#f0b74155]";
const navIdleClass = "text-soft hover:bg-[var(--bg-soft)]";
const navMenuPanelClass =
  "min-w-[220px] sm:w-64 max-h-[70vh] overflow-y-auto p-2 rounded-xl border border-[var(--line)] bg-[var(--bg-panel)] shadow-[var(--shadow)]";

function matchesNavPath(path: string, pathname: string): boolean {
  return pathname === path || pathname.startsWith(`${path}/`);
}

/** Map a pathname to the matching nav id */
function activeId(pathname: string): PageId {
  const explicit = ACTIVE_ID_ROUTES.find((route) => matchesNavPath(route.prefix, pathname));
  if (explicit) return explicit.id;
  const match = navItems.find((item) => matchesNavPath(item.path, pathname));
  return match ? match.id : "dashboard";
}

export default function Navigation({ walletAddress, onLogout, isPublic = false }: NavigationProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const [logoutLoading, setLogoutLoading] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeAccount, setActiveAccountState] = useState<Account | null>(
    getActiveAccountSelection,
  );
  const menuContainerRef = useRef<HTMLDivElement | null>(null);
  const moreContainerRef = useRef<HTMLDivElement | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const { isAdmin } = useAuthStore();
  const { openLoginModal } = useLoginModal();
  const currentPage = activeId(location.pathname);

  const isNavItemVisible = (item: NavItem): boolean => {
    if (isPublic) return PUBLIC_NAV_IDS.has(item.id);
    if (!isAdmin) return item.id !== "debug";
    return true;
  };

  const visibleGroups = navGroups
    .map((group) => ({
      group: group.group,
      items: group.items.filter((item) => isNavItemVisible(item)),
    }))
    .filter((group) => group.items.length > 0);

  const activeGroup = visibleGroups.find((group) =>
    group.items.some((item) => item.id === currentPage),
  );
  const currentGroup: NavGroup | undefined = activeGroup ?? visibleGroups[0];
  const otherGroups = visibleGroups.filter((group) => group !== currentGroup);
  const brandDestination = isPublic ? "/markets" : "/";
  const brandLabel = isPublic ? "Go to markets" : "Go to dashboard";

  // The switcher mirrors the shared store so the Settings page and any
  // other consumer stay in sync without a context provider.
  useEffect(() => onActiveAccountChange(setActiveAccountState), []);

  useEffect(() => {
    if (isPublic) return;
    let cancelled = false;
    accountService
      .listAccounts()
      .then((list) => {
        if (cancelled) return;
        setAccounts(list);
        // Drop a selection whose account no longer exists (deleted, or a
        // different wallet logged in) rather than sending a dead header.
        const stored = getActiveAccountSelection();
        if (stored && !list.some((a) => a.id === stored.id)) {
          setActiveAccount(null);
        }
      })
      .catch(() => {
        // Sub-accounts are opt-in: a disabled flag or an older backend
        // simply leaves the switcher hidden.
      });
    return () => {
      cancelled = true;
    };
  }, [isPublic]);

  const shortAddress = walletAddress
    ? `${walletAddress.slice(0, 6)}...${walletAddress.slice(-4)}`
    : "";

  useEffect(() => {
    setShowMenu(false);
    setShowMore(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!showMenu && !showMore) return;

    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target as Node;
      if (menuContainerRef.current?.contains(target)) return;
      if (moreContainerRef.current?.contains(target)) return;
      setShowMenu(false);
      setShowMore(false);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowMenu(false);
        setShowMore(false);
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
  }, [showMenu, showMore]);

  const handleLogoutConfirm = async () => {
    try {
      setLogoutLoading(true);
      await onLogout();
      setShowLogoutConfirm(false);
    } finally {
      setLogoutLoading(false);
    }
  };

  const openLogoutConfirm = () => {
    setShowMenu(false);
    setShowLogoutConfirm(true);
  };

  const handleLogoutCancel = () => {
    if (logoutLoading) return;
    setShowMenu(false);
    setShowLogoutConfirm(false);
  };

  // Nothing stored yet: show the personal account as selected, without
  // persisting it — the backend resolves an absent header the same way.
  const personalAccount = accounts.find((a) => a.kind === "personal") ?? null;
  const selectedAccount = activeAccount ?? personalAccount;
  const showAccountSwitcher = !isPublic && accounts.length > 0;

  const handleAccountSwitch = (value: string) => {
    const next = accounts.find((a) => String(a.id) === value) ?? null;
    setActiveAccount(next);
    setActiveAccountState(next);
  };

  return (
    <>
      <nav className="relative isolate z-40 border-b border-[var(--line)] bg-[#121722]/90 backdrop-blur">
        <div className="max-w-7xl mx-auto px-4">
          <div className="flex items-center justify-between gap-2 min-h-[4rem] py-2 flex-nowrap lg:flex-wrap">
            <div className="flex items-center gap-2 min-w-0">
              <div ref={menuContainerRef} className="relative flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setShowMenu((prev) => !prev)}
                  className="p-3 min-h-[44px] min-w-[44px] flex items-center justify-center rounded-md text-soft hover:bg-[var(--bg-soft)] transition lg:hidden"
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
                    navigate(brandDestination);
                    setShowMenu(false);
                    setShowMore(false);
                  }}
                  className="text-xl font-extrabold theme-brand hover:opacity-90 transition"
                  aria-label={brandLabel}
                >
                  POLYMARKET AI
                </button>

                {showMenu && (
                  <div
                    id="primary-nav-menu"
                    role="menu"
                    aria-label="Primary navigation"
                    className={`absolute left-0 top-full mt-2 lg:hidden ${resolveZ("navMenu")} ${navMenuPanelClass}`}
                  >
                    {/* Wallet address — mobile menu header (desktop shows it inline) */}
                    {!isPublic && shortAddress && (
                      <div className="px-3 pt-1 pb-2 text-[11px] text-muted mono border-b border-[var(--line)] mb-1">
                        {shortAddress}
                      </div>
                    )}
                    {visibleGroups.map((group) => (
                      <div key={group.group}>
                        <div className="px-3 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
                          {group.group}
                        </div>
                        {group.items.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              navigate(item.path);
                              setShowMenu(false);
                            }}
                            className={`block w-full text-left px-3 py-3 min-h-[44px] ${navItemBaseClass} ${
                              currentPage === item.id ? navActiveClass : navIdleClass
                            }`}
                          >
                            {item.label}
                          </button>
                        ))}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div
                ref={moreContainerRef}
                className="relative hidden lg:flex lg:items-center lg:gap-1 lg:flex-wrap min-w-0"
              >
                {currentGroup?.items.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => navigate(item.path)}
                    aria-current={currentPage === item.id ? "page" : undefined}
                    className={`px-3 py-2 min-h-[44px] whitespace-nowrap ${navItemBaseClass} ${
                      currentPage === item.id ? navActiveClass : navIdleClass
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
                {otherGroups.length > 0 && (
                  <div className="relative">
                    <button
                      type="button"
                      onClick={() => setShowMore((prev) => !prev)}
                      className={`px-3 py-2 min-h-[44px] whitespace-nowrap ${navItemBaseClass} ${navIdleClass}`}
                      aria-label="More navigation"
                      aria-expanded={showMore}
                      aria-controls="nav-more-menu"
                      aria-haspopup="menu"
                    >
                      More
                      <svg
                        className="w-4 h-4 ml-1"
                        viewBox="0 0 24 24"
                        fill="none"
                        xmlns="http://www.w3.org/2000/svg"
                        aria-hidden="true"
                      >
                        <path
                          d="M6 9L12 15L18 9"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                    {showMore && (
                      <div
                        id="nav-more-menu"
                        role="menu"
                        aria-label="More navigation"
                        className={`absolute left-0 top-full mt-2 ${resolveZ("navMenu")} ${navMenuPanelClass}`}
                      >
                        {otherGroups.map((group) => (
                          <div key={group.group}>
                            <div className="px-3 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
                              {group.group}
                            </div>
                            {group.items.map((item) => (
                              <button
                                key={item.id}
                                type="button"
                                role="menuitem"
                                onClick={() => {
                                  navigate(item.path);
                                  setShowMore(false);
                                }}
                                aria-current={currentPage === item.id ? "page" : undefined}
                                className={`block w-full text-left px-3 py-3 min-h-[44px] ${navItemBaseClass} ${
                                  currentPage === item.id ? navActiveClass : navIdleClass
                                }`}
                              >
                                {item.label}
                              </button>
                            ))}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
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
                  {showAccountSwitcher && (
                    <select
                      value={selectedAccount ? String(selectedAccount.id) : ""}
                      onChange={(e) => handleAccountSwitch(e.target.value)}
                      className="input-field text-sm py-1.5 min-h-[44px] w-auto max-w-[9rem]"
                      aria-label="Active trading account"
                      title="Trading account — subsequent requests send X-Account-Id"
                    >
                      {accounts.map((account) => (
                        <option key={account.id} value={String(account.id)}>
                          {account.kind === "personal" ? "Personal" : account.name}
                        </option>
                      ))}
                    </select>
                  )}

                  <NotificationCenter />

                  <div className="hidden sm:flex items-center px-3 py-2 rounded-md text-sm text-soft border border-[var(--line)] bg-[var(--bg-elevated)] mono">
                    {shortAddress}
                  </div>

                  {/* Logout Button */}
                  <button onClick={openLogoutConfirm} className="btn-danger">
                    Logout
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </nav>

      <ConfirmModal
        open={showLogoutConfirm}
        title="Confirm Logout"
        body="Are you sure you want to end your session now?"
        destructive
        confirmLabel="Log out"
        confirmDisabled={logoutLoading}
        onConfirm={handleLogoutConfirm}
        onCancel={handleLogoutCancel}
      />
    </>
  );
}
