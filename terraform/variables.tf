################################################################################
# Root Variables
################################################################################

variable "region" {
  description = "AWS region"
  type        = string
  default     = "ca-central-1"
}

variable "aws_profile" {
  description = "AWS CLI profile to use"
  type        = string
  default     = "is-staging-mfa"
}

variable "cluster_name" {
  description = "Name of the EKS cluster. Must be unique per concurrent cluster (see terraform/envs/*.tfvars: is-chaos-bench-a, is-chaos-bench-b, is-chaos-ml). All child resource names, IAM role names, and tags derive from this value so that multiple clusters can coexist in one account/region."
  type        = string
  default     = "is-chaos-benchmark"
}

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.31"
}

################################################################################
# Network
################################################################################

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones"
  type        = number
  default     = 3
}

################################################################################
# Node Group
################################################################################

variable "node_instance_types" {
  description = "Instance types for the node group. Single type only (m5.xlarge) so the rerun controls for instance-class heterogeneity, which a peer reviewer flagged as a confound in the original mixed SPOT pool (m5.xlarge/m5a.xlarge/m4.xlarge)."
  type        = list(string)
  default     = ["m5.xlarge"]
}

variable "capacity_type" {
  description = "EKS managed node group capacity type. ON_DEMAND for the rerun (the original run used SPOT, which a peer reviewer flagged as an uncontrolled confound alongside the mixed instance pool)."
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.capacity_type)
    error_message = "capacity_type must be either ON_DEMAND or SPOT."
  }
}

variable "node_desired_size" {
  description = "Desired number of worker nodes"
  type        = number
  default     = 3
}

variable "node_min_size" {
  description = "Minimum number of worker nodes"
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Maximum number of worker nodes"
  type        = number
  default     = 4
}

################################################################################
# S3 (experiment data backup)
################################################################################

variable "s3_bucket_name" {
  description = "S3 bucket for experiment data backup. Leave empty to auto-derive 'is-chaos-artifacts-<account_id>' (globally unique, stable across all three cluster workspaces in the same account). Only one of the three clusters should set create_s3_bucket = true; the others write into the same bucket under their own cluster-name key prefix (see scripts/export-to-s3.sh)."
  type        = string
  default     = ""
}

variable "create_s3_bucket" {
  description = "Whether this stack/workspace owns (creates) the shared artifacts bucket. Exactly one of the three cluster tfvars (envs/bench-a.tfvars, envs/bench-b.tfvars, envs/ml.tfvars) should set this to true; the others must be false to avoid three workspaces fighting over the same bucket name/state."
  type        = bool
  default     = true
}

################################################################################
# Access
################################################################################

variable "cluster_admin_arns" {
  description = "IAM ARNs to grant EKS cluster admin access"
  type        = list(string)
}

################################################################################
# Tags
################################################################################

variable "tags" {
  description = "Additional tags for all resources"
  type        = map(string)
  default     = {}
}
