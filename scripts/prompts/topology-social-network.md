# DeathStarBench Social Network: Topology Summary

Factual summary derived from `DeathStarBench/socialNetwork/docker-compose.yml`
and the C++ service handlers (`src/*/*.h`, checked via each handler's
`ClientPool<ThriftClient<...>>` constructor arguments) in this repo's
DeathStarBench checkout. Covers the 10 application services present in
`experiments/fault-space.yaml`'s `metadata.services` (plus text-service,
which sits on the call graph but is not itself a fault-space target).
Backing stores (per-service MongoDB/Redis/Memcached) and Jaeger are omitted:
no fault candidate targets them.

## Entry point

- **nginx-thrift** (OpenResty + Lua): the only externally reachable HTTP
  gateway (port 8080). Routes every API call to the matching backend Thrift
  RPC. Relevant routes: compose post -> compose-post-service, read home
  timeline -> home-timeline-service, read user timeline ->
  user-timeline-service, login/register/follow -> user-service /
  social-graph-service.

## Services and their downstream RPC calls

- **compose-post-service**: orchestrates a compose-post request. Calls
  post-storage-service, user-timeline-service, user-service,
  unique-id-service, media-service, text-service, and home-timeline-service
  (7 downstream calls per post; the highest fan-out node in the graph).
- **home-timeline-service**: calls post-storage-service and
  social-graph-service (resolves the caller's follow list before fetching
  post bodies).
- **user-timeline-service**: calls post-storage-service.
- **social-graph-service**: calls user-service.
- **user-service**: calls social-graph-service (bidirectional edge).
- **text-service** (not a fault-space target itself): calls
  url-shorten-service and user-mention-service.
- **post-storage-service, media-service, unique-id-service,
  url-shorten-service**: leaf services, no downstream Thrift calls; each
  backed only by its own MongoDB/Memcached. post-storage-service,
  media-service, and unique-id-service are reachable only via
  compose-post-service; url-shorten-service is reachable only via
  text-service.

## What this implies for fault selection

- Faulting **compose-post-service** or **nginx-thrift** affects the write
  path broadly: compose-post-service has the highest fan-out of any service,
  and nginx-thrift is the single entry point for every request type.
- Faulting a **leaf service** (post-storage, media, unique-id, url-shorten)
  isolates the blast radius to whichever single upstream caller depends on
  it.
- Faulting **social-graph-service** or **user-service** exercises their
  bidirectional dependency and can surface cascading effects in both
  compose-post-service (via user-service) and home-timeline-service (direct
  social-graph-service call).
- **home-timeline-service** and **user-timeline-service** sit on read paths;
  their failure modes (stale or empty timelines, elevated read latency)
  differ qualitatively from compose-post-service's write-path failures.
