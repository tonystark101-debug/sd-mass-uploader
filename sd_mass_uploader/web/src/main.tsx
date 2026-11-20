import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { captureToken } from "./api";
import { App } from "./App";
import { NoticeProvider } from "./ui";
import "./styles.css";

captureToken();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <NoticeProvider>
      <App />
    </NoticeProvider>
  </StrictMode>,
);
