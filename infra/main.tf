terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile
}

# ---------------------------------------------------------------------------
# Default VPC (already exists in every AWS account)
# ---------------------------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ---------------------------------------------------------------------------
# Security Group — allows 5432 from anywhere (dev only)
# ---------------------------------------------------------------------------
resource "aws_security_group" "rds" {
  name        = "${var.project}-${var.stage}-rds"
  description = "Allow PostgreSQL access for dev"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "PostgreSQL from anywhere (dev)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project}-${var.stage}-rds"
    Project = var.project
    Stage   = var.stage
  }
}

# ---------------------------------------------------------------------------
# DB Subnet Group (uses default subnets)
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "default" {
  name       = "${var.project}-${var.stage}-db-subnet"
  subnet_ids = data.aws_subnets.default.ids

  tags = {
    Name    = "${var.project}-${var.stage}-db-subnet"
    Project = var.project
    Stage   = var.stage
  }
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL
# ---------------------------------------------------------------------------
resource "aws_db_instance" "postgres" {
  identifier     = "apro-click-${var.stage}"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.default.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  publicly_accessible    = true
  skip_final_snapshot    = true
  deletion_protection    = false
  backup_retention_period = 1
  multi_az               = false

  tags = {
    Name    = "apro-click-${var.stage}"
    Project = var.project
    Stage   = var.stage
  }
}
