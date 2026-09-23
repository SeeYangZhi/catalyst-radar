// Registers jest-dom matchers (toBeInTheDocument, toHaveTextContent, …)
// on vitest's expect, including the TypeScript Assertion augmentation.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// RTL only auto-cleans when test globals are enabled; we import explicitly,
// so unmount rendered trees between tests ourselves.
afterEach(cleanup);
