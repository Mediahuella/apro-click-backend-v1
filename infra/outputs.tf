output "rds_endpoint" {
  description = "RDS endpoint (host:port)"
  value       = aws_db_instance.postgres.endpoint
}

output "rds_hostname" {
  description = "RDS hostname only"
  value       = aws_db_instance.postgres.address
}

output "rds_port" {
  description = "RDS port"
  value       = aws_db_instance.postgres.port
}

output "database_url" {
  description = "Full DATABASE_URL for .env"
  value       = "postgresql://${var.db_username}:${var.db_password}@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${var.db_name}?sslmode=require"
  sensitive   = true
}

output "security_group_id" {
  description = "Security group ID (for Lambda VPC config)"
  value       = aws_security_group.rds.id
}

output "chat_attachments_bucket" {
  description = "S3 bucket name for chat attachments"
  value       = aws_s3_bucket.chat_attachments.bucket
}
