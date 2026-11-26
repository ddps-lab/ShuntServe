output "head_instance_public_ip" {
  description = "Public IP of the head instance (SSH here to run the evaluation)"
  value       = module.shunt-head-instance.public_ip
}

output "head_instance_private_ip" {
  description = "Private IP of the head instance"
  value       = module.shunt-head-instance.private_ip
}

output "instance_public_ips" {
  description = "Map of public IPs by instance type and index"
  value = {
    for key, instance in module.shunt-worker-instance :
    key => instance.public_ip
  }
}

output "instance_private_ips" {
  description = "Map of private IPs by instance type and index"
  value = {
    for key, instance in module.shunt-worker-instance :
    key => instance.private_ip
  }
}

# Same shape as "nodes" in ../config.json; consumed by fill_config.py
output "nodes" {
  description = "Worker private IPs grouped by instance type"
  value = {
    for instance_type, count in var.instance_type_count :
    instance_type => [
      for i in range(count) : module.shunt-worker-instance["${instance_type}-${i}"].private_ip
    ]
  }
}
