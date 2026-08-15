# data-v2/ — revision-campaign experiment output

Real, analysis-eligible data only: `bench-a/`, `bench-b/`, `ml/`, each with
`{tool}/{scenario}/run-N.json` (+ `.timeseries.json.gz` sidecars),
`overhead/{config}/run-N.json`, and `campaigns/{strategy}/campaign-N/`.

**Never put one-off pilot, smoke-test, or validation runs under this tree.**
Their sole purpose is proving a protocol change works before trusting real
data to it, and any output shaped like `{cluster}/{tool}/{scenario}/run-N.json`
is indistinguishable from real data to a wildcard glob (e.g.
`data-v2/*/chaos-mesh/*/run-*.json`) — see `pilots/` at the repo root instead,
which no analysis-tree glob can ever match.

Tainted/excluded data (protocol violations caught mid-collection, kept for
the paper's methodology narrative, never fed to analysis) lives in
`overhead-tainted-128Mi/` and `overhead-noreset-200rps/` under each cluster.
