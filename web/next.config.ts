import type { NextConfig } from "next";

// The browser talks only to this app; requests to /api/v1 are passed on to the
// FastAPI backend, so there is one address and no cross-origin setup.
const API_URL = process.env.INCIDENT_AGENT_API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // The Docker image runs a self-contained server (see Dockerfile).
  output: process.env.NEXT_STANDALONE === "1" ? "standalone" : undefined,
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_URL}/api/v1/:path*` }];
  },
};

export default nextConfig;
