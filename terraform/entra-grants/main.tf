# The catalog's grants as Entra ID group memberships: one azuread_group_member per person and
# entra-group resource they hold today, through a role or directly. An expired grant drops out
# at the next plan.
#
# Not authoritative: a member added by hand stays in the group. permissions-ui lists those as
# "access nobody granted" so someone decides. A membership that already exists is imported
# rather than created, so pointing this at groups that already have members needs no
# preparation.

terraform {
  # for_each on import blocks.
  required_version = ">= 1.7"
  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 3.0"
    }
  }
}

locals {
  guid = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

  resources = merge([for f in fileset(var.catalog_dir, "resources/*.y*ml") : { for r in yamldecode(file("${var.catalog_dir}/${f}")) : r.id => r }]...)
  roles     = merge([for f in fileset(var.catalog_dir, "roles/*.y*ml") : { for r in yamldecode(file("${var.catalog_dir}/${f}")) : r.id => r }]...)
  people    = [for f in fileset(var.catalog_dir, "grants/*.y*ml") : yamldecode(file("${var.catalog_dir}/${f}"))]

  # A grant counts through the whole of its expiry day, in UTC.
  active = flatten([
    for person in local.people : [
      for grant in try(person.grants, []) : merge(grant, { person = lower(person.person) })
      if try(grant.expires, null) == null || timecmp(plantimestamp(), "${grant.expires}T23:59:59Z") <= 0
    ]
  ])

  pairs = {
    for m in distinct(flatten([
      for grant in local.active : [
        for rid in(try(grant.role, null) != null ? local.roles[grant.role].resources : [grant.resource]) :
        { person = grant.person, resource = rid } if local.resources[rid].kind == "entra-group"
      ]
    ])) : "${m.resource}/${m.person}" => m
  }

  targets = toset([for m in values(local.pairs) : local.resources[m.resource].target])
  users   = toset([for m in values(local.pairs) : m.person])
}

data "azuread_group" "this" {
  for_each = local.targets

  object_id        = can(regex(local.guid, each.value)) ? each.value : null
  display_name     = can(regex(local.guid, each.value)) ? null : each.value
  security_enabled = true
}

data "azuread_user" "this" {
  for_each = local.users

  user_principal_name = each.value
}

locals {
  ids = {
    for key, m in local.pairs : key => {
      group  = data.azuread_group.this[local.resources[m.resource].target].object_id
      member = data.azuread_user.this[m.person].object_id
    }
  }
}

import {
  for_each = { for key, id in local.ids : key => id if contains(data.azuread_group.this[local.resources[local.pairs[key].resource].target].members, id.member) }

  to = azuread_group_member.this[each.key]
  id = "${each.value.group}/member/${each.value.member}"
}

resource "azuread_group_member" "this" {
  for_each = local.ids

  group_object_id  = each.value.group
  member_object_id = each.value.member
}
