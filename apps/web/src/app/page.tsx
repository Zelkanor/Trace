"use client";

import { useState } from "react";
import { ApiStatus, checkApiHealth } from "../lib/api-health";

export default function Web() {
  const [status, setStatus] = useState<ApiStatus>("idle");
  const [error, setError] = useState<string | undefined>();

  const onCheck = async () => {
    setError(undefined);
    setStatus("checking");
    try {
      await checkApiHealth();
      setStatus("reachable");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      setStatus("unreachable");
    }
  };

  return (
    <div>
      <h1>Web</h1>
      <button type="button" onClick={onCheck} disabled={status === "checking"}>
        Check API
      </button>
      <p role="status">
        {status === "idle" && "API not checked yet"}
        {status === "checking" && "Checking API…"}
        {status === "reachable" && "API reachable"}
        {status === "unreachable" && `API unreachable: ${error}`}
      </p>
    </div>
  );
}
