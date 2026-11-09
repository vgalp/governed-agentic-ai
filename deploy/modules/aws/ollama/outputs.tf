output "private_ip" {
  value = aws_instance.ollama.private_ip
}

output "host" {
  description = "OLLAMA_HOST for the services."
  value       = "http://${aws_instance.ollama.private_ip}:11434"
}
