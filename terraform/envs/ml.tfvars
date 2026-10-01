################################################################################
# Cluster: is-chaos-ml
# Terraform workspace: ml
# Cluster for the Component 3 fault-selection campaigns. Writes into the S3
# bucket created by bench-a, under its own cluster-name key prefix.
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

# m5.2xlarge (8 vCPU): with one DeathStarBench copy per slot plus the chaos
# tool and monitoring pods, m5.xlarge nodes had too little unrequested CPU to
# schedule the wrk2 job (500m request).
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

# Extra IAM ARNs to get EKS cluster-admin access. The identity that creates
# the cluster gets it through the bootstrap access entry.
cluster_admin_arns = []

