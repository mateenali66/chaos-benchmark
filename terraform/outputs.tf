################################################################################
# Outputs
################################################################################

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = module.eks.cluster_endpoint
}

output "cluster_version" {
  description = "Kubernetes version"
  value       = module.eks.cluster_version
}

output "kubeconfig_command" {
  description = "Command to update kubeconfig"
  value       = module.eks.kubeconfig_command
}

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "s3_bucket" {
  description = "S3 bucket for experiment data (shared across all three cluster workspaces; resolves to the same name whether or not this workspace owns/creates it)"
  value       = local.s3_bucket_name
}

output "account_id" {
  description = "AWS account ID (for reference)"
  value       = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}
