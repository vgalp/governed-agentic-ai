# The audit key and OPA's token, generated here and kept in Secrets Manager with the environment's key.

resource "random_id" "audit_key" {
  byte_length = 32
}

resource "random_id" "opa_token" {
  byte_length = 24
}

locals {
  values = {
    audit-hmac-key = random_id.audit_key.hex # signs every audit event and checkpoint (ADR 002, 030)
    opa-token      = random_id.opa_token.hex # the only caller OPA answers (ADR 030)
  }
}

resource "aws_secretsmanager_secret" "secret" {
  for_each                = local.values
  name                    = "${var.name}/${each.key}"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 30 # a deleted audit key would make the whole audit chain unverifiable
}

resource "aws_secretsmanager_secret_version" "secret" {
  for_each      = local.values
  secret_id     = aws_secretsmanager_secret.secret[each.key].id
  secret_string = each.value
}
