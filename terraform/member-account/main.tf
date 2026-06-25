# ──────────────────────────────────────────────
# CCoE EC2 Scheduler - Member Account Setup
# ──────────────────────────────────────────────
# This Terraform configuration must be deployed in each member account
# where EC2 instances need to be managed.
#
# It creates:
#   1. IAM Role: CCoE-EC2Scheduler-Role
#      - Trusted by the management account
#      - Grants ec2:DescribeInstances, ec2:StopInstances, ec2:StartInstances
#      - Restricted to instances with environment tag ∈ {sandbox, dev, desarrollo}
#   2. CloudWatch Logs destination for Lambda logging
#
# Deployment options:
#   a) CloudFormation StackSets from management account (recommended)
#   b) Terraform per account
#   c) Manual via AWS Console
#
# ⚠️  IMPORTANT: Replace MANAGEMENT_ACCOUNT_ID in trust policy
# ──────────────────────────────────────────────

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ──────────────────────────────────────────────
# Variables
# ──────────────────────────────────────────────

variable "management_account_id" {
  description = "AWS Account ID of the Organizations management account"
  type        = string

  validation {
    condition     = can(regex("^\\d{12}$", var.management_account_id))
    error_message = "management_account_id must be a 12-digit AWS account ID."
  }
}

variable "role_name" {
  description = "Name of the IAM role to create in the member account"
  type        = string
  default     = "CCoE-EC2Scheduler-Role"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {
    Name        = "CCoE-EC2Scheduler-Role"
    team        = "ccoe"
    environment = "management"
    managed-by  = "terraform"
  }
}

# ──────────────────────────────────────────────
# IAM Role for Cross-Account Access
# ──────────────────────────────────────────────

resource "aws_iam_role" "scheduler_role" {
  name = var.role_name

  # Trust policy: only the management account can assume this role
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.management_account_id}:root"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = "CCoE-EC2Scheduler"
          }
        }
      }
    ]
  })

  tags = var.tags
}

# ──────────────────────────────────────────────
# IAM Policy for EC2 Management
# ──────────────────────────────────────────────

resource "aws_iam_policy" "scheduler_policy" {
  name        = "CCoE-EC2Scheduler-Policy"
  description = "Grants CCoE Scheduler permission to manage EC2 instances with environment tags"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2DescribeInstances"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeTags",
          "ec2:DescribeRegions"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2ManageInstances"
        Effect = "Allow"
        Action = [
          "ec2:StopInstances",
          "ec2:StartInstances"
        ]
        Resource = "*"
        Condition = {
          StringEqualsIgnoreCase = {
            "aws:ResourceTag/environment" : ["sandbox", "dev", "desarrollo"]
          }
        }
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "scheduler_policy_attach" {
  role       = aws_iam_role.scheduler_role.name
  policy_arn = aws_iam_policy.scheduler_policy.arn
}

# ──────────────────────────────────────────────
# Outputs
# ──────────────────────────────────────────────

output "role_name" {
  description = "Name of the IAM role created"
  value       = aws_iam_role.scheduler_role.name
}

output "role_arn" {
  description = "ARN of the IAM role created"
  value       = aws_iam_role.scheduler_role.arn
}