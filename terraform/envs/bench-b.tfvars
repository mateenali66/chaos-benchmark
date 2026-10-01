################################################################################
# Cluster: is-chaos-bench-b
# Terraform workspace: bench-b
# One of two identical clusters for Components 1 and 2 (paired with
# is-chaos-bench-a). Runs LitmusChaos reps 1-15, then Chaos Mesh reps 16-30.
# Writes into the S3 bucket created by bench-a, under its own cluster-name
# key prefix.
#
# Usage:
#   terraform -chdir=terraform workspace select bench-b || terraform -chdir=terraform workspace new bench-b
#   terraform -chdir=terraform plan  -var-file=envs/bench-b.tfvars
#   terraform -chdir=terraform apply -var-file=envs/bench-b.tfvars
################################################################################

cluster_name = "is-chaos-bench-b"

region = "ca-central-1"

# Non-overlapping /16 across all three clusters (bench-a/bench-b/ml):
#   bench-a: 10.10.0.0/16
#   bench-b: 10.20.0.0/16
#   ml:      10.30.0.0/16
vpc_cidr = "10.20.0.0/16"
az_count = 3

node_instance_types = ["m5.xlarge"]
capacity_type       = "ON_DEMAND"
# 3 nodes as last applied. For Component 1 the node group was scaled to 9
# nodes to host three slots (scripts/SLOT_PARALLELISM.md).
node_desired_size = 3
node_min_size       = 3
node_max_size       = 3

# bench-a owns the shared bucket; this stack must NOT also try to create it.
create_s3_bucket = false

tags = {
  Environment = "bench-b"
}

# Extra IAM ARNs to get EKS cluster-admin access. The identity that creates
# the cluster gets it through the bootstrap access entry.
cluster_admin_arns = []

