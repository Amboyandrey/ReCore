import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits a minimal server bundle (server.js + only the deps it needs) for the Docker image.
  output: "standalone",
};

export default nextConfig;
