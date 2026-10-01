################################################################################
# Cluster: is-chaos-bench-a
# Terraform workspace: bench-a
# One of two identical clusters for Components 1 and 2 (paired with
# is-chaos-bench-b). Runs Chaos Mesh reps 1-15, then LitmusChaos reps 16-30.
# Owns the shared S3 artifacts bucket (create_s3_bucket = true), see
# terraform/main.tf and scripts/export-to-s3.sh.
#
# Usage:
#   terraform -chdir=terraform workspace select bench-a || terraform -chdir=terraform workspace new bench-a
#   terraform -chdir=terraform plan  -var-file=envs/bench-a.tfvars
#   terraform -chdir=terraform apply -var-file=envs/bench-a.tfvars
################################################################################

cluster_name = "is-chaos-bench-a"

region = "ca-central-1"

# Non-overlapping /16 across all three clusters (bench-a/bench-b/ml):
#   bench-a: 10.10.0.0/16
#   bench-b: 10.20.0.0/16
#   ml:      10.30.0.0/16
vpc_cidr = "10.10.0.0/16"
az_count = 3

node_instance_types = ["m5.xlarge"]
capacity_type       = "ON_DEMAND"
# 3 nodes as last applied. For Component 1 the node group was scaled to 9
# nodes to host three slots (scripts/SLOT_PARALLELISM.md).
node_desired_size = 3
node_min_size       = 3
node_max_size       = 3

# This cluster owns/creates the shared is-chaos-artifacts-<account_id> bucket.
# Exactly one of the three tfvars files must set this to true.
create_s3_bucket = true

tags = {
  Environment = "bench-a"
}

# Extra IAM ARNs to get EKS cluster-admin access. The identity that creates
# the cluster gets it through the bootstrap access entry.
cluster_admin_arns = []

