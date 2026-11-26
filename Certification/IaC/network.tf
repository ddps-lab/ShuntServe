module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.6"

  name = "${var.prefix}-shunt-cert-vpc"
  cidr = "192.168.0.0/16"

  azs                     = [var.availability_zone]
  public_subnets          = ["192.168.0.0/24"]
  map_public_ip_on_launch = true
}

module "security-group" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "~> 5.3"

  name   = "${var.prefix}-shunt-cert-security-group"
  vpc_id = module.vpc.vpc_id
  ingress_with_cidr_blocks = [
    {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = "0.0.0.0/0"
    }
  ]
  ingress_with_self = [
    {
      from_port = 0
      to_port   = 0
      protocol  = "-1"
    }
  ]
  egress_with_cidr_blocks = [
    {
      from_port   = 0
      to_port     = 0
      protocol    = "-1"
      cidr_blocks = "0.0.0.0/0"
    }
  ]
}
