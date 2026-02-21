################################################################################
# Chaos Benchmark Infrastructure
# EKS cluster for chaos engineering experiments (Paper 4)
################################################################################

locals {
  tags = merge(var.tags, {
    Cluster = var.cluster_name
  })
}

################################################################################
# VPC
################################################################################

module "vpc" {
  source = "./modules/vpc"

  name               = var.cluster_name
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  cluster_name       = var.cluster_name
  enable_nat_gateway = true
  single_nat_gateway = true

  tags = local.tags
}

################################################################################
# EKS Cluster
################################################################################

module "eks" {
  source = "./modules/eks"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids

  node_instance_types = var.node_instance_types
  node_desired_size   = var.node_desired_size
  node_min_size       = var.node_min_size
  node_max_size       = var.node_max_size

  cluster_admin_arns     = var.cluster_admin_arns
  cluster_log_retention_days = 7

  tags = local.tags
}

################################################################################
# EKS Add-ons
################################################################################

module "eks_addons" {
  source = "./modules/eks-addons"

  cluster_name    = module.eks.cluster_name
  cluster_version = var.cluster_version

  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url

  enable_ebs_csi_driver = true

  tags = local.tags
}

################################################################################
# S3 Bucket (experiment data backup)
################################################################################

resource "aws_s3_bucket" "data" {
  bucket = var.s3_bucket_name

  tags = merge(local.tags, {
    Name = var.s3_bucket_name
  })
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
