#!/usr/bin/env -S python3 -u
"""ChaosCenter GraphQL client for the Litmus LLM fault-selection arm.

Component 3's llm strategy needs to "select and launch" pre-registered
experiments via the same operation surface the litmuschaos/litmus-mcp-server
exposes (registerInfra, createChaosExperiment, runChaosExperiment,
getExperimentRun), per the design decision in ../jss/ML_ARM_DESIGN.md and
the R2 review response. This module re-implements that same GraphQL surface
directly in Python rather than shelling out to the Go MCP binary, because:

  1. The MCP server authenticates with a single static JWT (24h TTL) passed
     at process startup; a multi-day campaign needs re-authentication, which
     the MCP server does not support. This client re-logs-in transparently
     on a 401 or when the cached token is near expiry.
  2. All mutation/query shapes below were extracted directly from
     litmuschaos/litmus-mcp-server's handlers.go (commit 45acf1a7, the same
     commit ML_ARM_DESIGN.md pins) and verified against the live ChaosCenter
     instance on is-chaos-ml -- this is not a guess at the schema, it is the
     same surface the MCP server itself calls, just with our own token
     lifecycle wrapped around it.

Chaos infrastructure was registered interactively (see
litmus-credentials.json for the environment_id / infra_id / project_id).
This client assumes that registration already exists; it does not perform
it (registerInfra requires a Referer header and manifest-apply step that is
a one-time setup action, not a per-call operation).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "litmus-credentials.json"

# Fault names known to be pre-registered as ChaosExperiment CRDs (see
# post-deploy.sh) and therefore valid faultName values for
# createChaosExperiment's faults[] entries.
KNOWN_FAULT_NAMES = {
    "pod-delete", "container-kill", "pod-network-latency", "pod-network-loss",
    "pod-network-partition", "pod-cpu-hog-exec", "pod-memory-hog-exec",
    "pod-http-status-code",
}

# Terminal Argo Workflow phases (getExperimentRun's "phase" field).
TERMINAL_PHASES = {"Completed", "Succeeded", "Failed", "Error", "Skipped"}


class ChaosCenterError(RuntimeError):
    pass


class ChaosCenterClient:
    def __init__(self, credentials_path: Path = CREDENTIALS_PATH,
                 graphql_url: str | None = None, auth_url: str | None = None):
        self.creds = json.loads(credentials_path.read_text())
        # Callers running from outside the cluster (e.g. this laptop via
        # kubectl port-forward) must pass localhost URLs explicitly; the
        # credentials file's *_endpoint_* fields are the in-cluster DNS
        # names, only reachable from a pod running inside is-chaos-ml.
        self.graphql_url = graphql_url or f"http://localhost:{self.creds['graphql_service_local_port']}/query"
        self.auth_url = auth_url or f"http://localhost:{self.creds['auth_service_local_port']}"
        self.project_id = self.creds["project_id"]
        self.infra_id = self.creds["infra_id"]
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- auth -----------------------------------------------------------

    def _login(self) -> None:
        body = json.dumps({
            "username": self.creds["admin_username"],
            "password": self.creds["admin_password"],
        }).encode()
        req = urllib.request.Request(
            f"{self.auth_url}/login", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        self._token = data["accessToken"]
        # Refresh 5 minutes before the server's own expiry, not exactly at it.
        self._token_expires_at = time.time() + data["expiresIn"] - 300

    def _ensure_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            self._login()
        return self._token

    # -- graphql ----------------------------------------------------------

    def _graphql(self, query: str, variables: dict, retry_on_auth_error: bool = True) -> dict:
        token = self._ensure_token()
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            self.graphql_url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "x-litmus-project-id": self.project_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry_on_auth_error:
                self._token = None
                return self._graphql(query, variables, retry_on_auth_error=False)
            raise ChaosCenterError(f"HTTP {exc.code}: {exc.read().decode()[:500]}") from exc
        if result.get("errors"):
            msg = result["errors"][0].get("message", str(result["errors"]))
            if "unauthorized" in msg.lower() and retry_on_auth_error:
                self._token = None
                return self._graphql(query, variables, retry_on_auth_error=False)
            raise ChaosCenterError(f"GraphQL error: {msg}")
        return result["data"]

    # -- experiments --------------------------------------------------------

    def create_chaos_experiment(self, name: str, fault_name: str, target_service: str,
                                 env: dict, description: str = "") -> str:
        """Register a single-fault experiment (Argo Workflow manifest, same
        shape litmus-mcp-server's createChaosExperiment builds) and return
        its experimentID. Idempotency is the caller's responsibility (this
        always creates a new experiment; see register_fault_space_experiments
        for the "skip if a same-named one already exists" wrapper)."""
        if fault_name not in KNOWN_FAULT_NAMES:
            raise ValueError(f"fault_name {fault_name!r} is not a pre-registered "
                              f"ChaosExperiment CRD: {sorted(KNOWN_FAULT_NAMES)}")
        env_list = [{"name": k, "value": str(v)} for k, v in env.items()]
        env_list.append({"name": "TARGET_PODS", "value": target_service})
        manifest = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": name, "namespace": "litmus"},
            "spec": {
                "entrypoint": "chaos-experiment",
                "templates": [
                    {"name": "chaos-experiment",
                     "steps": [[{"name": fault_name, "template": fault_name}]]},
                    {"name": fault_name,
                     "container": {
                         "image": "litmuschaos/go-runner:latest",
                         "command": ["chaos-runner"],
                         "args": ["-name", fault_name],
                         "env": env_list,
                     }},
                ],
            },
        }
        mutation = """
            mutation($projectID: ID!, $request: ChaosExperimentRequest!) {
                createChaosExperiment(request: $request, projectID: $projectID) {
                    experimentID
                }
            }
        """
        variables = {
            "projectID": self.project_id,
            "request": {
                "experimentName": name,
                "experimentDescription": description or f"Fault-space candidate: {name}",
                "infraID": self.infra_id,
                "experimentManifest": json.dumps(manifest),
                "cronSyntax": "none",
                "isCustomExperiment": True,
                "weightages": [{"faultName": fault_name, "weightage": 10}],
                "tags": ["chaos-benchmark", "component-3"],
            },
        }
        data = self._graphql(mutation, variables)
        return data["createChaosExperiment"]["experimentID"]

    def list_experiments(self) -> list[dict]:
        query = """
            query($projectID: ID!) {
                listExperiment(projectID: $projectID, request: {}) {
                    totalNoOfExperiments
                    experiments { experimentID experimentType }
                }
            }
        """
        data = self._graphql(query, {"projectID": self.project_id})
        return data["listExperiment"]["experiments"]

    def run_chaos_experiment(self, experiment_id: str) -> str:
        """Trigger a pre-registered experiment. Returns notifyID (used to
        poll get_experiment_run)."""
        mutation = """
            mutation($projectID: ID!, $experimentID: String!) {
                runChaosExperiment(experimentID: $experimentID, projectID: $projectID) {
                    notifyID
                }
            }
        """
        data = self._graphql(mutation, {"projectID": self.project_id, "experimentID": experiment_id})
        return data["runChaosExperiment"]["notifyID"]

    def get_experiment_run(self, notify_id: str) -> dict:
        query = """
            query($projectID: ID!, $notifyID: ID) {
                getExperimentRun(projectID: $projectID, notifyID: $notifyID) {
                    experimentRunID
                    phase
                    resiliencyScore
                    faultsPassed
                    faultsFailed
                    faultsAwaited
                    faultsStopped
                    faultsNa
                    totalFaults
                    updatedAt
                    createdAt
                }
            }
        """
        data = self._graphql(query, {"projectID": self.project_id, "notifyID": notify_id})
        return data["getExperimentRun"]

    def wait_for_completion(self, notify_id: str, timeout_s: int = 300, poll_interval_s: int = 5) -> dict:
        """Poll get_experiment_run until phase is terminal or timeout_s
        elapses. Returns the final run dict; phase may be a non-terminal
        value if timeout_s was reached (caller should check)."""
        deadline = time.time() + timeout_s
        run = {}
        while time.time() < deadline:
            run = self.get_experiment_run(notify_id)
            if run.get("phase") in TERMINAL_PHASES:
                return run
            time.sleep(poll_interval_s)
        return run
