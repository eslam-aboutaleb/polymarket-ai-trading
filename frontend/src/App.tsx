import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "./store/authStore";
import { LoginModalProvider } from "./context/LoginModalContext";
import LoginPage from "./pages/LoginPage";
import SettingsPage from "./pages/SettingsPage";
import AnalysisPage from "./pages/AnalysisPage";
import AppLayout from "./components/AppLayout";
import PublicAppLayout from "./components/PublicAppLayout";
import Dashboard from "./components/Dashboard";
import Markets from "./components/Markets";
import Opportunities from "./components/Opportunities";
import Leaderboard from "./components/Leaderboard";
import TradeHistory from "./components/TradeHistory";
import CopyTrading from "./components/CopyTrading";
import MarketMaking from "./components/MarketMaking";
import Backtesting from "./components/Backtesting";
import News from "./components/News";
import UserGuide from "./components/UserGuide";
import DebugDashboard from "./components/DebugDashboard";
import OpsCenter from "./components/OpsCenter";
import { authService } from "./services/authService";

function SessionLoadingScreen() {
  return (
    <div className="app-shell min-h-screen flex items-center justify-center px-4">
      <div className="surface-panel p-6 text-center">
        <p className="text-soft text-sm">Restoring secure session...</p>
      </div>
    </div>
  );
}

// Protected route wrapper that waits for server-validated session bootstrap.
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, sessionReady } = useAuthStore();
  if (!sessionReady) return <SessionLoadingScreen />;

  if (!isAuthenticated) {
    return <Navigate to="/markets" replace />;
  }

  return <>{children}</>;
}

/** Only renders children when the authenticated user is an admin. */
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAdmin } = useAuthStore();
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function App() {
  const {
    isAuthenticated,
    sessionReady,
    setSession,
    setSessionReady,
    clearSession,
  } = useAuthStore();

  useEffect(() => {
    let cancelled = false;

    try {
      localStorage.removeItem("authStore");
    } catch {
      // Ignore private mode/localStorage restrictions.
    }

    setSessionReady(false);

    authService
      .getCurrentUser()
      .then((user) => {
        if (cancelled) return;
        setSession({
          walletAddress: user.wallet_address,
          isAdmin: user.is_admin,
        });
      })
      .catch(() => {
        if (cancelled) return;
        clearSession();
      })
      .finally(() => {
        if (!cancelled) {
          setSessionReady(true);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [setSession, setSessionReady, clearSession]);

  return (
    <LoginModalProvider>
      <BrowserRouter>
        <div className="app-shell">
          <Routes>
            <Route
              path="/login"
              element={
                !sessionReady ? (
                  <SessionLoadingScreen />
                ) : isAuthenticated ? (
                  <Navigate to="/" replace />
                ) : (
                  <LoginPage />
                )
              }
            />

            {/* Public pages — accessible without login */}
            <Route element={<PublicAppLayout />}>
              <Route path="markets" element={<Markets />} />
              <Route path="opportunities" element={<Opportunities />} />
              <Route path="leaderboard" element={<Leaderboard />} />
              <Route path="copy-trading" element={<CopyTrading />} />
              <Route path="guide" element={<UserGuide />} />
              <Route path="analysis" element={<AnalysisPage />} />
            </Route>

            {/* Authenticated pages — require wallet login */}
            <Route
              element={
                <ProtectedRoute>
                  <AppLayout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Dashboard />} />
              <Route path="market-making" element={<MarketMaking />} />
              <Route path="backtesting" element={<Backtesting />} />
              <Route path="news" element={<News />} />
              <Route path="trades" element={<TradeHistory />} />
              <Route path="ops" element={<OpsCenter />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route
                path="debug"
                element={
                  <AdminRoute>
                    <DebugDashboard />
                  </AdminRoute>
                }
              />
            </Route>

            <Route path="*" element={<Navigate to="/markets" replace />} />
          </Routes>
        </div>
      </BrowserRouter>
    </LoginModalProvider>
  );
}

export default App;
