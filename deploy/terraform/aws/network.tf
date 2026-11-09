# A VPC with private subnets only for everything that runs. Public subnets exist only to hold the
# NAT gateway when internet_egress = true; nothing that serves requests is ever placed in them.
# AWS services (ECR, logs, Secrets Manager, KMS, STS, Systems Manager, S3) through VPC endpoints.

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  azs          = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.vpc_cidr]
  account      = data.aws_caller_identity.current.account_id
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Keep the VPC's default security group empty: nothing may use it by accident.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

# --- outbound only, when allowed ------------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  count  = var.internet_egress ? 1 : 0
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "nat" {
  count             = var.internet_egress ? 1 : 0
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 250)
  availability_zone = local.azs[0]
  tags              = { Name = "${var.name}-nat-only" }
}

resource "aws_route_table" "nat" {
  count  = var.internet_egress ? 1 : 0
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main[0].id
  }
  tags = { Name = "${var.name}-nat-only" }
}

resource "aws_route_table_association" "nat" {
  count          = var.internet_egress ? 1 : 0
  subnet_id      = aws_subnet.nat[0].id
  route_table_id = aws_route_table.nat[0].id
}

resource "aws_eip" "nat" {
  count  = var.internet_egress ? 1 : 0
  domain = "vpc"
}

resource "aws_nat_gateway" "main" {
  count         = var.internet_egress ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.nat[0].id
  tags          = { Name = var.name }
}

resource "aws_route" "egress" {
  count                  = var.internet_egress ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

# --- VPC endpoints --------------------------------------------------------------------------------

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-endpoints"
  description = "AWS service endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(["ecr.api", "ecr.dkr", "logs", "secretsmanager", "kms", "sts", "ssm", "ssmmessages", "ec2messages"])
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${var.name}-${each.key}" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.name}-s3" }
}

# --- flow logs: who talked to whom, kept like the service logs ------------------------------------

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/${var.name}/vpc-flow"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_iam_role" "flow" {
  name = "${var.name}-vpc-flow"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "vpc-flow-logs.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "flow" {
  role = aws_iam_role.flow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow.arn}:*"
    }]
  })
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow.arn
  iam_role_arn    = aws_iam_role.flow.arn
}
