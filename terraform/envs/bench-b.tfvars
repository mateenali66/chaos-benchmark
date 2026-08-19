################################################################################
# Cluster: is-chaos-bench-b
# Terraform workspace: bench-b
# One of two identical benchmark clusters running the Chaos Mesh / LitmusChaos
# comparison concurrently (paired with is-chaos-bench-a). Does NOT own the
# shared S3 artifacts bucket (create_s3_bucket = false) -- it writes into the
# bucket created by bench-a, under its own cluster-name key prefix.
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
# Corrected 2026-08-18: was 9, inconsistent with max_size below (AWS caps desired
# at max on apply); the actually-provisioned/applied cluster ran 3 nodes, matching
# analysis/PREREGISTRATION.md and terraform.tfstate.d/bench-b's applied state.
node_desired_size = 3
node_min_size       = 3
node_max_size       = 3

# bench-a owns the shared bucket; this stack must NOT also try to create it.
create_s3_bucket = false

tags = {
  Environment = "bench-b"
}

# REQUIRED before `terraform plan`: fill in with the IAM ARN(s) that should
# get EKS cluster-admin access (e.g. your is-staging-mfa role/user ARN).
cluster_admin_arns = []  # creator user/mateen gets AmazonEKSClusterAdminPolicy automatically via the bootstrap access entry; list ADDITIONAL admins only

aws_profile = "is-staging-mfa"
