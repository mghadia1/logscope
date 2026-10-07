import "@fontsource-variable/instrument-sans/wght.css";
import "@fontsource/fragment-mono/latin-400.css";
import "@fontsource/martian-mono/latin-500.css";
import "@fontsource/martian-mono/latin-600.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
