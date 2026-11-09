output "file_system_id" {
  value = aws_efs_file_system.state.id
}

output "file_system_arn" {
  value = aws_efs_file_system.state.arn
}

output "access_point_id" {
  value = aws_efs_access_point.state.id
}

output "access_point_arn" {
  value = aws_efs_access_point.state.arn
}
