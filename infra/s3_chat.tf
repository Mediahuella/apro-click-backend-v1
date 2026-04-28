# ---------------------------------------------------------------------------
# S3 — adjuntos del chat (imágenes, Excel, PDF, etc.)
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "chat_attachments" {
  bucket = "${var.project}-${var.stage}-chat-attachments"

  tags = {
    Name    = "${var.project}-${var.stage}-chat-attachments"
    Project = var.project
    Stage   = var.stage
  }
}

resource "aws_s3_bucket_cors_configuration" "chat_attachments" {
  bucket = aws_s3_bucket.chat_attachments.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "GET"]
    # Restringir a tu dominio real en producción
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "chat_attachments" {
  bucket = aws_s3_bucket.chat_attachments.id

  rule {
    id     = "expire-temp-uploads"
    status = "Enabled"

    filter { prefix = "tmp/" }

    expiration { days = 1 }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "chat_attachments" {
  bucket = aws_s3_bucket.chat_attachments.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "chat_attachments" {
  bucket = aws_s3_bucket.chat_attachments.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
