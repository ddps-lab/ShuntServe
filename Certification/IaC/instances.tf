locals {
  hf_user_data = "#!/bin/bash\necho 'HF_TOKEN=${var.hf_token}' >> /etc/environment\necho 'export HF_TOKEN=${var.hf_token}' >> /home/ubuntu/.bashrc"

  # "g6.12xlarge-0" => "g6.12xlarge", ...
  instances = merge([
    for instance_type, count in var.instance_type_count : {
      for i in range(count) :
      "${instance_type}-${i}" => instance_type
    }
  ]...)
}

module "shunt-head-instance" {
  source  = "terraform-aws-modules/ec2-instance/aws"
  version = "~> 6.2"

  name = "${var.prefix}-shunt-cert-head-instance"

  ami                    = var.ami_id
  instance_type          = var.head_instance_type
  key_name               = var.key_name
  monitoring             = true
  subnet_id              = module.vpc.public_subnets[0]
  vpc_security_group_ids = [module.security-group.security_group_id]

  user_data            = local.hf_user_data
  iam_instance_profile = aws_iam_instance_profile.s3-instance-profile.name

  root_block_device = {
    volume_size = var.root_volume_size
  }

  tags = {
    Name = "${var.prefix}-shunt-cert-head-instance"
  }
}

module "shunt-worker-instance" {
  source  = "terraform-aws-modules/ec2-instance/aws"
  version = "~> 6.2"

  for_each = local.instances

  name                   = "${var.prefix}-shunt-cert-worker-${each.key}"
  ami                    = var.ami_id
  instance_type          = each.value
  key_name               = var.key_name
  monitoring             = true
  subnet_id              = module.vpc.public_subnets[0]
  vpc_security_group_ids = [module.security-group.security_group_id]

  user_data            = local.hf_user_data
  iam_instance_profile = aws_iam_instance_profile.s3-instance-profile.name

  root_block_device = {
    volume_size = var.root_volume_size
  }

  tags = {
    Name         = "${var.prefix}-shunt-cert-worker-${each.key}"
    InstanceType = each.value
  }
}
