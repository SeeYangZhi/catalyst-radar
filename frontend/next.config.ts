import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root so an unrelated parent lockfile is not picked up.
  turbopack: {
    root: path.join(import.meta.dirname),
  },
};

export default nextConfig;
