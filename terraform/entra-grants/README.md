# entra-grants

Applies a permissions-ui catalog to Entra ID: every person gets the group memberships their
grants give them today, through a role or directly. Run it from the catalog repository's
pipeline, on merge; permissions-ui merges a request once it is approved.

```hcl
module "grants" {
  source      = "github.com/MagmaMoose/permissions-ui//terraform/entra-grants?ref=<release tag>"
  catalog_dir = "${path.root}/access"
}

# Memberships people already hold are adopted, not created again. An import block may only sit in
# the root module, so it is here and not in the module.
import {
  for_each = module.grants.import_ids
  to       = module.grants.azuread_group_member.this[each.key]
  id       = each.value
}
```

- Only `entra-group` resources become memberships. A resource's `target` is the group's display
  name or object id.
- Not authoritative: memberships added by hand stay. permissions-ui shows them as access nobody
  granted.
- Memberships that already exist are imported by the block above, not created, so an existing
  group adopts cleanly.
- Works with azuread 2.x and 3.x.
- An expired grant drops out on the next plan. Run the plan daily (a CronJob, or the
  `permissions-ui expire` job, which merges a removal and so triggers your pipeline).
- The identity that applies it needs `GroupMember.ReadWrite.All` and `User.Read.All` on
  Microsoft Graph, or Groups Administrator scoped to an administrative unit holding the groups.
