import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest does not expose globals here, so Testing Library cannot register
// its own cleanup; unmount each test's components explicitly.
afterEach(cleanup);
