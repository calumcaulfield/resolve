import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

import type { NextConfig } from "next"

const config: NextConfig = {
  // `standalone` produces a minimal server bundle for the Docker image.
  output: "standalone",
  reactStrictMode: true,
  // A lockfile exists further up the filesystem; Next has to be told which one
  // bounds the standalone trace, or it warns and guesses.
  outputFileTracingRoot: dirname(fileURLToPath(import.meta.url)),
  // Deliberately no `env` block: RESOLVE_API_URL and RESOLVE_API_KEY are read
  // at request time in server components, not inlined at build time. That
  // keeps the API key out of the client bundle and lets one image run against
  // any environment.
}

export default config
