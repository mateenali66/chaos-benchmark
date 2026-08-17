################################################################################
# Cluster: is-chaos-ml
# Terraform workspace: ml
# Third identical cluster, reserved for the ML/LLM-driven fault-selection arm
# (Path A rework track -- see papers/paper4-chaos-engineering/CLAUDE.md).
# Does NOT own the shared S3 artifacts bucket (create_s3_bucket = false) -- it
# writes into the bucket created by bench-a, under its own cluster-name key
# prefix.
#
# Usage:
#   terraform -chdir=terraform workspace select ml || terraform -chdir=terraform workspace new ml
#   terraform -chdir=terraform plan  -var-file=envs/ml.tfvars
#   terraform -chdir=terraform apply -var-file=envs/ml.tfvars
################################################################################

cluster_name = "is-chaos-ml"

region = "ca-central-1"

# Non-overlapping /16 across all three clusters (bench-a/bench-b/ml):
#   bench-a: 10.10.0.0/16
#   bench-b: 10.20.0.0/16
#   ml:      10.30.0.0/16
vpc_cidr = "10.30.0.0/16"
az_count = 3

# m5.xlarge (4 vCPU) undersized once litmus/monitoring/gremlin/chaos-testing
# infra pods share the node with a full 27-pod DSB deployment per slot --
# found live 2026-08-16 when the wrk2 load-generator job (500m CPU request)
# couldn't schedule ("0/3 nodes are available: 3 Insufficient cpu") with
# nodes already at 97-99% CPU request from DSB+infra alone. Upsized to
# m5.2xlarge (8 vCPU) for real headroom rather than starving wrk2's request.
node_instance_types = ["m5.2xlarge"]
capacity_type       = "ON_DEMAND"
node_desired_size   = 3
node_min_size       = 3
node_max_size       = 3

# bench-a owns the shared bucket; this stack must NOT also try to create it.
create_s3_bucket = false

tags = {
  Environment = "ml"
}

# REQUIRED before `terraform plan`: fill in with the IAM ARN(s) that should
# get EKS cluster-admin access (e.g. your is-staging-mfa role/user ARN).
cluster_admin_arns = []  # creator user/mateen gets AmazonEKSClusterAdminPolicy automatically via the bootstrap access entry; list ADDITIONAL admins only

aws_profile = "is-staging-mfa"
