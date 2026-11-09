# Shared files for the services: audit checkpoints and worker health. Encrypted, TLS only, mounted
# through one access point as the image's user, and reachable from the services' security group only.

resource "aws_efs_file_system" "state" {
  encrypted  = true
  kms_key_id = var.kms_key_arn
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
  vpc_id      = var.vpc_id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [var.apps_security_group_id]
  }
}

resource "aws_efs_mount_target" "state" {
  count           = length(var.subnet_ids)
  file_system_id  = aws_efs_file_system.state.id
  subnet_id       = var.subnet_ids[count.index]
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
