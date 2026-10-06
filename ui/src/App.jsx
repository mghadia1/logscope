import { useEffect, useState } from "react";

// Milestone 0: a shell that shows whether the API is reachable. Search,
// results and the histogram arrive in Milestone 5.
export default function App() {
  const [health, setHealth] = useState({ state: "checking" });

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((body) => {
        if (!cancelled) setHealth({ state: "ok", version: body.version });
      })
      .catch((error) => {
        if (!cancelled) setHealth({ state: "down", error: error.message });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="app">
      <header>
        <h1>LogScope</h1>
        <p className="tagline">A log search engine, built from the storage up.</p>
      </header>
      <p className={`api api-${health.state}`} role="status">
        {health.state === "checking" && "Checking the API…"}
        {health.state === "ok" && `API ok, version ${health.version}`}
        {health.state === "down" && `API unreachable (${health.error})`}
      </p>
    </main>
  );
}
