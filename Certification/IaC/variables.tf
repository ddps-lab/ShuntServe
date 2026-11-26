variable "prefix" {
  type        = string
  description = "The prefix to use for the infrastructure"
}

variable "region" {
  type        = string
  description = "The AWS region to deploy the infrastructure"
  default     = "us-west-2"
}

variable "availability_zone" {
  type        = string
  description = "The availability zone for all instances (all nodes share one subnet)"
  default     = "us-west-2a"
}

variable "awscli_profile" {
  type        = string
  description = "The AWS CLI profile to use"
}

variable "ami_id" {
  type        = string
  description = "The AMI ID with ShuntServe installed at /home/ubuntu/ShuntServe"
}

variable "key_name" {
  type        = string
  description = "The key name to use for the infrastructure"
}

variable "hf_token" {
  type        = string
  description = "HuggingFace API token for accessing gated models (e.g. Llama)"
  sensitive   = true
}

variable "head_instance_type" {
  type        = string
  description = "The instance type for the head node (runs the optimizer and GlobalServer)"
  default     = "m5.xlarge"
}

variable "root_volume_size" {
  type        = number
  description = "The root EBS volume size in GB for EC2 instances"
  default     = 300
}

# Evaluation cluster: 3 GPU types, 8 nodes. Must match "nodes" in ../config.json.
variable "instance_type_count" {
  type        = map(number)
  description = "The number of worker instances per instance type"
  default = {
    "g6.12xlarge" = 2 # 4x L4 each
    "g5.12xlarge" = 0 # 4x A10G each
    "g6e.xlarge"  = 0 # 1x L40S each
  }
}
