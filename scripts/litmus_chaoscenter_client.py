#!/usr/bin/env -S python3 -u
"""ChaosCenter GraphQL client.

Used by register-chaoscenter-experiments.py to register the 36 fault-space
candidates, and by run-campaign.py's optional catalog check. No campaign
injection runs through ChaosCenter: the LLM arms call Amazon Bedrock
directly and every injection is applied from a local manifest. The
LitmusChaos MCP server was not used either. Its experiment-creation tool is
disabled at commit 45acf1a7, and the workflow manifest its handler builds
calls a `chaos-runner` command that the litmuschaos/go-runner image does not
contain. create_chaos_experiment builds the same manifest, so the registered
experiments serve as a catalog only. See the erratum in
analysis/PREREGISTRATION.md. run_chaos_experiment, get_experiment_run and
wait_for_completion are not called anywhere in this repository.

The query and mutation shapes follow litmus-mcp-server's handlers.go at
commit 45acf1a7. The client logs in again on a 401 or shortly before the
token expires.

The login and the project and infrastructure ids come from
litmus-credentials.json, which is not committed. Registering the chaos infrastructure is a one-time
manual step and is not done here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "litmus-credentials.json"

# Fault names installed as ChaosExperiment resources by post-deploy.sh, and
# so valid faultName values for createChaosExperiment.
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
        # Defaults are localhost URLs on the credentials file's local ports,
        # for use through kubectl port-forward. The file's *_endpoint_*
        # fields are in-cluster DNS names and are not used here.
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
        """Register a single-fault experiment and return its experimentID.

        The Argo Workflow manifest has the shape litmus-mcp-server builds,
        including its `chaos-runner` command (see the module docstring). This
        always creates a new experiment; register-chaoscenter-experiments.py
        skips candidates that are already registered.
        """
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

    def list_experiments(self, page_size: int = 100) -> list[dict]:
        """Return every registered experiment in the project.

        Without an explicit pagination block ChaosCenter returns only 15
        experiments, so this requests pages of page_size until
        totalNoOfExperiments is reached.
        """
        query = """
            query($projectID: ID!, $page: Int!, $limit: Int!) {
                listExperiment(projectID: $projectID,
                                request: {pagination: {page: $page, limit: $limit}}) {
                    totalNoOfExperiments
                    experiments { experimentID experimentType }
                }
            }
        """
        experiments: list[dict] = []
        page = 0
        while True:
            data = self._graphql(query, {
                "projectID": self.project_id, "page": page, "limit": page_size,
            })
            result = data["listExperiment"]
            experiments.extend(result["experiments"])
            if len(experiments) >= result["totalNoOfExperiments"] or not result["experiments"]:
                break
            page += 1
        return experiments

    def run_chaos_experiment(self, experiment_id: str) -> str:
        """Trigger a registered experiment and return its notifyID for get_experiment_run."""
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
        """Poll get_experiment_run until the phase is terminal or timeout_s passes.

        Returns the last run dict. Its phase is not terminal if the timeout
        was reached.
        """
        deadline = time.time() + timeout_s
        run = {}
        while time.time() < deadline:
            run = self.get_experiment_run(notify_id)
            if run.get("phase") in TERMINAL_PHASES:
                return run
            time.sleep(poll_interval_s)
        return run
