/**
 * Browser entry point that mounts <App /> into #root and loads the global stylesheet.
 *
 * @module main
 */

import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "react-query";
import App from "./App.tsx";
import { queryClient } from "./lib/queryClient";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
