import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required for static site hosting (generates the 'out' directory)
  output: "export",
  
  // E2E_DIST lets a throwaway production build use a separate output dir so it doesn't
  // collide with a running `next dev` that owns `.next`. Normal runs ignore it.
  ...(process.env.E2E_DIST ? { distDir: ".next-e2e" } : {}),
};

export default nextConfig;
