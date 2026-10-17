output "memberships" {
  description = "Every membership this applies, as <resource>/<person>"
  value       = sort(keys(local.pairs))
}
