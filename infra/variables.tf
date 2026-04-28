variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "AWS CLI profile"
  type        = string
  default     = "mh-prod"
}

variable "project" {
  description = "Project slug for naming"
  type        = string
  default     = "aproclick"
}

variable "stage" {
  description = "Environment stage"
  type        = string
  default     = "dev"
}

variable "db_username" {
  description = "Master username for RDS"
  type        = string
  default     = "postgres"
}

variable "db_password" {
  description = "Master password for RDS"
  type        = string
  sensitive   = true
}

variable "db_name" {
  description = "Initial database name"
  type        = string
  default     = "aproclick_dev"
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = "PostgreSQL engine version"
  type        = string
  default     = "17"
}

variable "db_allocated_storage" {
  description = "Storage in GB"
  type        = number
  default     = 20
}
