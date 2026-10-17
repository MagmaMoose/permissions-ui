mock_provider "azuread" {
  mock_data "azuread_group" {
    defaults = {
      object_id = "00000000-0000-0000-0000-0000000000a1"
      members   = []
    }
  }
  mock_data "azuread_user" {
    defaults = {
      object_id = "00000000-0000-0000-0000-0000000000b1"
    }
  }
}

variables {
  catalog_dir = "../../examples/catalog"
}

run "roles_expand_and_only_groups_count" {
  command = plan

  assert {
    condition = alltrue([for m in [
      "k8s-acc-payments-write/ada.lovelace@example.com",
      "k8s-prd-payments-read/ada.lovelace@example.com",
      "grafana-viewer/ada.lovelace@example.com",
      "payments-keyvault-acc/ada.lovelace@example.com",
      "grafana-viewer/grace.hopper@example.com",
    ] : contains(output.memberships, m)])
    error_message = "A role grant must give a membership for every entra-group resource in the role."
  }

  assert {
    condition     = length([for m in output.memberships : m if startswith(m, "vpn/") || startswith(m, "github-payments/")]) == 0
    error_message = "Manual and GitHub resources are not Entra ID memberships."
  }
}
