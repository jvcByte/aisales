import type { NextConfig } from "next";

// The dashboard is a client of the API and holds no state of its own, so the
// only configuration it needs is where the API lives. Rewriting rather than
// calling an absolute URL keeps the browser same-origin in development and
// means no CORS preflight on every poll.
const API = process.env.AISALES_API ?? "http://127.0.0.1:8100";

const config: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/:path*` }];
  },
};

export default config;
