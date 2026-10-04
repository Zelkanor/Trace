const HEALTH_TIMEOUT_MS = 3000;

export type ApiStatus = "idle" | "checking" | "reachable" | "unreachable";

type BootstrapHealth = {
  service: "trace-api";
  api_status: "ok";
  health_scope: "process";
};

function isBootstrapHealth(value: unknown): value is BootstrapHealth {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  return (
    body.service === "trace-api" &&
    body.api_status === "ok" &&
    body.health_scope === "process"
  );
}

/** Resolves if the API answers /health with the expected contract; throws otherwise. */
export async function checkApiHealth(): Promise<void> {
  // Must be referenced literally so Next.js can inline it at build time.
  const host = process.env.NEXT_PUBLIC_API_HOST;
  if (!host) throw new Error("NEXT_PUBLIC_API_HOST is not set");

  let response: Response;
  try {
    response = await fetch(new URL("/health", host), {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error(`No response within ${HEALTH_TIMEOUT_MS / 1000}s`);
    }
    throw new Error("Could not connect to the API");
  }

  // An error response can also carry valid JSON, so check the status first.
  if (!response.ok) throw new Error(`API responded with HTTP ${response.status}`);

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new Error("API response was not valid JSON");
  }
  if (!isBootstrapHealth(body)) {
    throw new Error("API response did not match the health contract");
  }
}
