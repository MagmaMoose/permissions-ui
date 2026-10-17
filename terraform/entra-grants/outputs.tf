output "memberships" {
  description = "Every membership this applies, as <resource>/<person>"
  value       = sort(keys(local.pairs))
}

output "import_ids" {
  description = "The memberships that already exist, keyed like azuread_group_member.this, for the root module's import block"
  value       = { for key, id in local.ids : key => "${id.group}/member/${id.member}" if contains(data.azuread_group.this[local.resources[local.pairs[key].resource].target].members, id.member) }
}
