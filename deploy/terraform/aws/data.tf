# One customer-managed key for everything stored (Kafka, logs, secrets, the registry, the shared
# files, the GPU host's disk), secrets, the image registry, and the shared files.

resource "aws_kms_key" "main" {
  description             = "${var.name}: Kafka, logs, secrets, images, files"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Account", Effect = "Allow", Principal = { AWS = "arn:aws:iam::${local.account}:root" }, Action = "kms:*", Resource = "*" },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = { ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.region}:${local.account}:*" } }
      },
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.main.key_id
}

# --- secrets --------------------------------------------------------------------------------------

resource "random_id" "audit_key" {
  byte_length = 32
}

resource "random_id" "opa_token" {
  byte_length = 24
}

locals {
  secret_values = {
    audit-hmac-key = random_id.audit_key.hex # signs every audit event and checkpoint (ADR 002, 030)
    opa-token      = random_id.opa_token.hex # the only caller OPA answers (ADR 030)
  }
}

resource "aws_secretsmanager_secret" "secret" {
  for_each                = local.secret_values
  name                    = "${var.name}/${each.key}"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 30 # a deleted audit key would make the whole audit chain unverifiable
}

resource "aws_secretsmanager_secret_version" "secret" {
  for_each      = local.secret_values
  secret_id     = aws_secretsmanager_secret.secret[each.key].id
  secret_string = each.value
}

# --- image registry -------------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "${var.name}/governed-agentic-ai"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

# --- shared files: audit checkpoints and worker health ---------------------------------------------

resource "aws_efs_file_system" "state" {
  encrypted  = true
  kms_key_id = aws_kms_key.main.arn
  tags       = { Name = "${var.name}-state" }
}

resource "aws_efs_file_system_policy" "state" {
  file_system_id = aws_efs_file_system.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TLSOnly"
      Effect    = "Deny"
      Principal = { AWS = "*" }
      Action    = "*"
      Resource  = aws_efs_file_system.state.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_security_group" "efs" {
  name        = "${var.name}-efs"
  description = "Shared files: NFS from the services only"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.apps.id]
  }
}

resource "aws_efs_mount_target" "state" {
  count           = var.az_count
  file_system_id  = aws_efs_file_system.state.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "state" {
  file_system_id = aws_efs_file_system.state.id
  posix_user {
    uid = 10001 # the image's "governed" user
    gid = 10001
  }
  root_directory {
    path = "/governed"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "750"
    }
  }
}
