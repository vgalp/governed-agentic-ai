# Kafka: Amazon MSK, TLS everywhere, IAM sign-in only (no unauthenticated access, no passwords).
# Each service's task role (apps module) gets the policy built here: exactly the topics that
# kafka_access gives it, and only its own consumer groups. Topics are created by the setup task
# (apps module), not on first use.

locals {
  cluster   = aws_msk_cluster.main.arn
  topic_arn = { for t in keys(var.kafka_access.topics) : t => "${replace(local.cluster, ":cluster/", ":topic/")}/${t}" }
  group_arn = replace(local.cluster, ":cluster/", ":group/")

  policies = { for s in var.services : s => {
    Version = "2012-10-17"
    Statement = concat(
      [{ Sid = "Connect", Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"], Resource = [local.cluster] }],
      length(var.kafka_access.services[s].write) == 0 ? [] : [
        { Sid = "IdempotentProducer", Effect = "Allow", Action = ["kafka-cluster:WriteDataIdempotently"], Resource = [local.cluster] },
        { Sid = "Write", Effect = "Allow", Action = ["kafka-cluster:WriteData", "kafka-cluster:DescribeTopic"],
        Resource = [for t in var.kafka_access.services[s].write : local.topic_arn[t]] },
      ],
      length(var.kafka_access.services[s].read) == 0 ? [] : [
        { Sid = "Read", Effect = "Allow", Action = ["kafka-cluster:ReadData", "kafka-cluster:DescribeTopic"],
        Resource = [for t in var.kafka_access.services[s].read : local.topic_arn[t]] },
      ],
      length(var.kafka_access.services[s].groups) == 0 ? [] : [
        { Sid = "ConsumerGroups", Effect = "Allow", Action = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"],
        Resource = [for g in var.kafka_access.services[s].groups : "${local.group_arn}/${g}${endswith(g, "-") ? "*" : ""}"] },
      ],
    )
  } }

  # The setup task (apps module) may create and configure this cluster's topics, and nothing else.
  setup_policy = {
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
  }
  az_count = length(var.subnet_ids)
}

resource "aws_security_group" "kafka" {
  name        = "${var.name}-kafka"
  description = "MSK brokers: IAM over TLS from the services only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "Kafka, IAM sign-in over TLS"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [var.apps_security_group_id]
  }
}

resource "aws_cloudwatch_log_group" "kafka" {
  name              = "/${var.name}/kafka"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_msk_configuration" "main" {
  name              = "${var.name}-kafka"
  kafka_versions    = [var.kafka_version]
  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=${min(3, local.az_count)}
    min.insync.replicas=${min(2, max(1, local.az_count - 1))}
    num.partitions=${var.partitions}
    unclean.leader.election.enable=false
  EOT
}

resource "aws_msk_cluster" "main" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = local.az_count

  broker_node_group_info {
    instance_type   = var.instance_type
    client_subnets  = var.subnet_ids
    security_groups = [aws_security_group.kafka.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.storage_gb
      }
    }
    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  client_authentication {
    unauthenticated = false
    sasl {
      iam   = true
      scram = false
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.main.arn
    revision = aws_msk_configuration.main.latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.kafka.name
      }
    }
  }
}

# The audit topics are kept for good, so the disks grow with them.
resource "aws_appautoscaling_target" "kafka_storage" {
  service_namespace  = "kafka"
  scalable_dimension = "kafka:broker-storage:VolumeSize"
  resource_id        = aws_msk_cluster.main.arn
  min_capacity       = 1
  max_capacity       = var.storage_max_gb
}

resource "aws_appautoscaling_policy" "kafka_storage" {
  name               = "${var.name}-kafka-storage"
  service_namespace  = aws_appautoscaling_target.kafka_storage.service_namespace
  scalable_dimension = aws_appautoscaling_target.kafka_storage.scalable_dimension
  resource_id        = aws_appautoscaling_target.kafka_storage.resource_id
  policy_type        = "TargetTrackingScaling"
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "KafkaBrokerStorageUtilization"
    }
    target_value = 60
  }
}
