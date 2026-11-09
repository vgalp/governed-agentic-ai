# The GPU host for Ollama: private subnet, no public IP, no SSH key. Port 11434 only from the
# services; administration through Systems Manager Session Manager (VPC endpoints in network.tf).

data "aws_ssm_parameter" "gpu_ami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
}

resource "aws_security_group" "ollama" {
  name        = "${var.name}-ollama"
  description = "Ollama: port 11434 from the services only"
  vpc_id      = aws_vpc.main.id
  ingress {
    description     = "Ollama has no sign-in of its own: only the services may reach it"
    from_port       = 11434
    to_port         = 11434
    protocol        = "tcp"
    security_groups = [aws_security_group.apps.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "ollama" {
  name = "${var.name}-ollama"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ollama_ssm" {
  role       = aws_iam_role.ollama.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ollama" {
  name = "${var.name}-ollama"
  role = aws_iam_role.ollama.name
}

resource "aws_instance" "ollama" {
  ami                         = data.aws_ssm_parameter.gpu_ami.value
  instance_type               = var.ollama_instance_type
  subnet_id                   = aws_subnet.private[0].id
  vpc_security_group_ids      = [aws_security_group.ollama.id]
  iam_instance_profile        = aws_iam_instance_profile.ollama.name
  associate_public_ip_address = false
  user_data = templatefile("${path.module}/../shared/ollama-cloud-init.sh.tftpl", {
    ollama_version = var.ollama_version
    models         = var.ollama_models
  })
  user_data_replace_on_change = true

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 200 # the models
    encrypted   = true
    kms_key_id  = aws_kms_key.main.arn
  }

  tags = { Name = "${var.name}-ollama" }

  lifecycle {
    ignore_changes = [ami] # a newer AMI is a deliberate replacement, not every apply
  }
}
