#!/usr/bin/env python3
"""
Generate experimental architecture diagram using the Python diagrams library.
https://diagrams.mingrammer.com

Outputs: analysis/figures/fig1_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import EKS
from diagrams.aws.network import VPC
from diagrams.k8s.compute import Pod, Deploy
from diagrams.k8s.network import SVC
from diagrams.k8s.ecosystem import Helm
from diagrams.onprem.monitoring import Prometheus, Grafana
from diagrams.onprem.tracing import Jaeger
from diagrams.onprem.client import User
from diagrams.onprem.compute import Server
from diagrams.programming.language import Python
from diagrams.generic.storage import Storage
from pathlib import Path

FIG_DIR = Path(__file__).parent / "figures"
FIG_DIR.mkdir(exist_ok=True)

graph_attr = {
    "fontsize": "14",
    "bgcolor": "white",
    "pad": "0.5",
    "ranksep": "1.0",
    "nodesep": "0.6",
}

with Diagram(
    "Chaos Engineering Benchmark Architecture",
    filename=str(FIG_DIR / "fig1_architecture"),
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    outformat="png",
):
    orchestrator = Python("run-experiment.py\n(Orchestrator)")

    with Cluster("AWS EKS Cluster (us-east-1)\n3x SPOT m5.xlarge"):

        with Cluster("social-network namespace\n(DeathStarBench)"):
            nginx = SVC("nginx-thrift\n(API Gateway)")
            compose = Deploy("compose-post")
            timeline = Deploy("home-timeline")
            user_svc = Deploy("user-service")
            other = Pod("+ 23 services")
            nginx >> compose
            nginx >> timeline
            nginx >> user_svc
            compose >> other

        with Cluster("Chaos Tools"):
            chaos_mesh = Helm("Chaos Mesh 2.8.1")
            litmus = Helm("LitmusChaos 3.26.0")

        with Cluster("monitoring namespace"):
            prom = Prometheus("Prometheus")
            grafana = Grafana("Grafana")
            jaeger = Jaeger("Jaeger")

        wrk2 = Pod("wrk2 Job\n(Load Generator)")

    data_store = Storage("JSON Results\n(120 files, 48 MB)")

    # Connections
    orchestrator >> Edge(label="kubectl apply/delete", style="dashed") >> chaos_mesh
    orchestrator >> Edge(label="kubectl apply/delete", style="dashed") >> litmus
    orchestrator >> Edge(label="create Job") >> wrk2
    orchestrator >> Edge(label="PromQL queries") >> prom
    orchestrator >> Edge(label="save results") >> data_store

    wrk2 >> Edge(label="HTTP traffic\n200 rps") >> nginx

    prom >> Edge(label="scrape metrics", style="dotted") >> nginx
    prom >> Edge(label="scrape", style="dotted") >> compose

    chaos_mesh >> Edge(label="inject faults", color="red", style="bold") >> compose
    litmus >> Edge(label="inject faults", color="red", style="bold") >> compose
