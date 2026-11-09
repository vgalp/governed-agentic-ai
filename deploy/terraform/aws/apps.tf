# The services on ECS Fargate, in the private subnets, without public IPs:
#
#   api       the web app, dashboard and /health, behind an internal load balancer
#   planner   the planner, the tool servers and OPA in one task (they share 127.0.0.1)
#   executor  the executor, the tool servers and OPA
#   setup     a one-off task that creates the Kafka topics (output: setup_command)
#
# Each runs with its own task role: Kafka access from ops/kafka_access.yaml, the shared files, and
# nothing else. One copy each: the executor rebuilds its state from Kafka and must not run twice.

resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_security_group" "apps" {
  name        = "${var.name}-apps"
  description = "The services: the web app from the load balancer; out to Kafka, Ollama, files and AWS endpoints"
  vpc_id      = aws_vpc.main.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "apps_from_lb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.apps.id
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.lb.id
}

resource "aws_cloudwatch_log_group" "svc" {
  for_each          = toset(concat(local.deployed, ["setup"]))
  name              = "/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# --- roles ----------------------------------------------------------------------------------------

locals {
  ecs_trust = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  efs_statement = {
    Sid       = "SharedFiles"
    Effect    = "Allow"
    Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    Resource  = aws_efs_file_system.state.arn
    Condition = { StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.state.arn } }
  }
}

# Starting containers: pull the image, write logs, read the two secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = local.ecs_trust
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [for s in aws_secretsmanager_secret.secret : s.arn] },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

# What each service may do while running.
resource "aws_iam_role" "svc" {
  for_each           = toset(local.deployed)
  name               = "${var.name}-${each.key}"
  assume_role_policy = local.ecs_trust
}

resource "aws_iam_role_policy" "svc_kafka" {
  for_each = toset(local.deployed)
  name     = "kafka"
  role     = aws_iam_role.svc[each.key].id
  policy   = jsonencode(local.kafka_policy[each.key])
}

resource "aws_iam_role_policy" "svc_files" {
  for_each = toset(local.deployed)
  name     = "shared-files"
  role     = aws_iam_role.svc[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [local.efs_statement] })
}

# The setup task may create and configure this cluster's topics, and nothing else.
resource "aws_iam_role" "setup" {
  name               = "${var.name}-kafka-setup"
  assume_role_policy = local.ecs_trust
}

resource "aws_iam_role_policy" "setup" {
  role = aws_iam_role.setup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"], Resource = local.cluster },
      {
        Effect = "Allow"
        Action = ["kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopic", "kafka-cluster:AlterTopic",
        "kafka-cluster:DescribeTopicDynamicConfiguration", "kafka-cluster:AlterTopicDynamicConfiguration"]
        Resource = values(local.topic_arn)
      },
    ]
  })
}

# --- tasks ----------------------------------------------------------------------------------------

locals {
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  opa = {
    command = ["opa", "run", "--server", "--addr", "127.0.0.1:8181", "--authentication=token", "--authorization=basic", "/app/policies"]
  }
  tools = { command = ["python", "-m", "ops.run_all"] }
  tasks = {
    api      = { cpu = 1024, memory = 2048, containers = { api = { command = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"], port = 8000 } } }
    planner  = { cpu = 2048, memory = 4096, containers = { planner = { command = ["python", "-m", "agents.planner"] }, tools = local.tools, opa = local.opa } }
    executor = { cpu = 1024, memory = 2048, containers = { executor = { command = ["python", "-m", "agents.executor"] }, tools = local.tools, opa = local.opa } }
  }
  common_env = merge({
    PROFILE                 = var.profile
    KAFKA_BOOTSTRAP_SERVERS = local.kafka_bootstrap
    KAFKA_SECURITY_PROTOCOL = "SASL_SSL"
    KAFKA_SASL_MECHANISM    = "OAUTHBEARER"
    KAFKA_OAUTH_PROVIDER    = "aws-msk-iam"
    KAFKA_CA_FILE           = "/etc/ssl/certs/ca-certificates.crt"
    AWS_REGION              = var.region
    OLLAMA_HOST             = "http://${aws_instance.ollama.private_ip}:11434"
    AUDIT_CHECKPOINT_DIR    = "/mnt/state/audit-checkpoints"
    GOVERNED_HEALTH_DIR     = "/mnt/state/health" # shared, so /health in the API sees the workers
    TOOL_SERVERS            = join(",", var.tool_servers)
  }, var.app_env)
  secrets = { AUDIT_HMAC_KEY = "audit-hmac-key", OPA_TOKEN = "opa-token" }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = var.deploy_apps ? local.tasks : {}
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.svc[each.key].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "state"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.state.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.state.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([for name, c in each.value.containers : {
    name         = name
    image        = local.image
    command      = c.command
    essential    = true
    environment  = [for k, v in local.common_env : { name = k, value = v }]
    secrets      = [for k, v in local.secrets : { name = k, valueFrom = aws_secretsmanager_secret.secret[v].arn }]
    portMappings = try([{ containerPort = c.port, protocol = "tcp" }], [])
    mountPoints  = [{ sourceVolume = "state", containerPath = "/mnt/state" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.svc[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = name
      }
    }
  }])
}

resource "aws_ecs_service" "svc" {
  for_each                           = var.deploy_apps ? local.tasks : {}
  name                               = each.key
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.svc[each.key].arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0 # never two executors at once
  deployment_maximum_percent         = 100
  enable_execute_command             = false

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.apps.id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.api.arn
      container_name   = "api"
      container_port   = 8000
    }
  }

  depends_on = [aws_lb_listener.api]
}

resource "aws_ecs_task_definition" "setup" {
  count                    = var.deploy_apps ? 1 : 0
  family                   = "${var.name}-kafka-setup"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.setup.arn
  container_definitions = jsonencode([{
    name      = "setup"
    image     = local.image
    command   = ["python", "-m", "ops.kafka_admin", "create-topics"]
    essential = true
    environment = [for k, v in {
      KAFKA_BOOTSTRAP_SERVERS = local.kafka_bootstrap, KAFKA_SECURITY_PROTOCOL = "SASL_SSL",
      KAFKA_SASL_MECHANISM    = "OAUTHBEARER", KAFKA_OAUTH_PROVIDER = "aws-msk-iam", AWS_REGION = var.region,
      KAFKA_CA_FILE           = "/etc/ssl/certs/ca-certificates.crt", KAFKA_PARTITIONS = tostring(var.partitions),
      KAFKA_REPLICATION       = tostring(min(3, var.az_count))
    } : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.svc["setup"].name, awslogs-region = var.region, awslogs-stream-prefix = "setup" }
    }
  }])
}

# --- the internal load balancer -------------------------------------------------------------------

resource "aws_security_group" "lb" {
  name        = "${var.name}-lb"
  description = "Internal load balancer: the web app for client_cidrs only"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port   = var.certificate_arn == "" ? 80 : 443
    to_port     = var.certificate_arn == "" ? 80 : 443
    protocol    = "tcp"
    cidr_blocks = local.client_cidrs
  }
  egress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_lb" "api" {
  name                       = "${var.name}-api"
  internal                   = true # a private address only
  load_balancer_type         = "application"
  subnets                    = aws_subnet.private[*].id
  security_groups            = [aws_security_group.lb.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path    = "/" # not /health: a Kafka outage must show as "down" there, not restart the web app
    matcher = "200"
  }
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.api.arn
  port              = var.certificate_arn == "" ? 80 : 443
  protocol          = var.certificate_arn == "" ? "HTTP" : "HTTPS"
  ssl_policy        = var.certificate_arn == "" ? null : "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn == "" ? null : var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
