/**
 * Class error boundary that catches render errors in its subtree and renders
 * a fallback with a reload button instead of white-screening the app.
 *
 * @module components/ErrorBoundary
 */
import { Component, ErrorInfo, ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("Dashboard render error:", error, errorInfo);
  }

  handleReload = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (error) {
      return (
        <div className="space-y-4 p-6 surface-panel">
          <h2 className="text-xl font-bold">Something went wrong</h2>
          <p className="text-soft text-sm">
            The dashboard hit an unexpected error and could not be rendered.
          </p>
          <p className="text-xs text-muted mono break-all">{error.message}</p>
          <div>
            <button onClick={this.handleReload} className="btn-muted">
              Reload Dashboard
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
