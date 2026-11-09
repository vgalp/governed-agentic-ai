# Kafka: Amazon MSK, TLS everywhere, IAM sign-in only (no unauthenticated access, no passwords).
# Each service's task role may read and write exactly the topics that ops/kafka_access.yaml gives it,
# and use only its own consumer groups. Topics are created by the setup task (apps.tf), not on first use.

locals {
  access   = yamldecode(file("${path.module}/../../../ops/kafka_access.yaml"))
  deployed = ["api", "planner", "executor"]
  cluster  = aws_msk_cluster.main.arn
  topic_arn = { for t in keys(local.access.topics) :
  t => "${replace(local.cluster, ":cluster/", ":topic/")}/${t}" }
  group_arn = replace(local.cluster, ":cluster/", ":group/")

  kafka_policy = { for s in local.deployed : s => {
    Version = "2012-10-17"
    Statement = concat(
      [{ Sid = "Connect", Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"], Resource = [local.cluster] }],
      length(flatten(try(local.access.services[s].write, []))) == 0 ? [] : [
        { Sid = "IdempotentProducer", Effect = "Allow", Action = ["kafka-cluster:WriteDataIdempotently"], Resource = [local.cluster] },
        { Sid = "Write", Effect = "Allow", Action = ["kafka-cluster:WriteData", "kafka-cluster:DescribeTopic"],
        Resource = [for t in flatten(local.access.services[s].write) : local.topic_arn[t]] },
      ],
      length(flatten(try(local.access.services[s].read, []))) == 0 ? [] : [
        { Sid = "Read", Effect = "Allow", Action = ["kafka-cluster:ReadData", "kafka-cluster:DescribeTopic"],
        Resource = [for t in flatten(local.access.services[s].read) : local.topic_arn[t]] },
      ],
      length(flatten(try(local.access.services[s].groups, []))) == 0 ? [] : [
        { Sid = "ConsumerGroups", Effect = "Allow", Action = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"],
        Resource = [for g in flatten(local.access.services[s].groups) : "${local.group_arn}/${g}${endswith(g, "-") ? "*" : ""}"] },
      ],
    )
  } }
  kafka_bootstrap = aws_msk_cluster.main.bootstrap_brokers_sasl_iam
}

resource "aws_security_group" "kafka" {
  name        = "${var.name}-kafka"
  description = "MSK brokers: IAM over TLS from the services only"
  vpc_id      = aws_vpc.main.id
  ingress {
    description     = "Kafka, IAM sign-in over TLS"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [aws_security_group.apps.id]
  }
}

resource "aws_cloudwatch_log_group" "kafka" {
  name              = "/${var.name}/kafka"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_msk_configuration" "main" {
  name              = "${var.name}-kafka"
  kafka_versions    = [var.kafka_version]
  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=${min(3, var.az_count)}
    min.insync.replicas=${min(2, var.az_count)}
    num.partitions=${var.partitions}
    unclean.leader.election.enable=false
  EOT
}

resource "aws_msk_cluster" "main" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.az_count

  broker_node_group_info {
    instance_type   = var.kafka_instance_type
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.kafka.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.kafka_storage_gb
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
    encryption_at_rest_kms_key_arn = aws_kms_key.main.arn
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
  max_capacity       = var.kafka_storage_max_gb
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
