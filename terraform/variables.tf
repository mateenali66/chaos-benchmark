################################################################################
# Root Variables
################################################################################

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile to use"
  type        = string
  default     = "personal"
}

variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
  default     = "chaos-benchmark"
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
  description = "Instance types for SPOT node group"
  type        = list(string)
  default     = ["m5.xlarge", "m5a.xlarge", "m4.xlarge"]
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
  description = "S3 bucket for experiment data backup"
  type        = string
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
