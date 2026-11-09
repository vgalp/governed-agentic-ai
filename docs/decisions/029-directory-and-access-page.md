# 029: Reading the organization's directory for the access page

Status: Accepted · Date: 2026-11-08

## Context
With sign-in (ADR 028), a person's role comes from their groups in the organization's identity
provider. That works without any copy of the directory: groups arrive in the token at sign-in, and
a person removed from a group loses the role at their next sign-in or token renewal. But nobody
could see the result. Which groups give which role, who is in them, and whether a mapped group even
exists were spread across the provider's admin screen, `sign_in.roles` and `roles.yaml`. Most
organizations run Microsoft Entra ID or Okta; Keycloak is the local test provider.

## Decision
A read-only directory connector, and an access page that puts the three layers side by side.

- **Connectors** (`auth/directory.py`): Keycloak's admin REST API (a confidential client whose
  service account has `view-users` and `query-groups`), Microsoft Graph for Entra ID (an app
  registration with `GroupMember.Read.All` and `User.ReadBasic.All`, optionally limited to listed
  group IDs), and Okta's management API (a read-only API token). All return groups with members,
  paged and bounded (500 groups, 2,000 members per group). Deactivated Okta users are left out.
- **Configuration only**: the profile's `sign_in.directory` names the provider and the
  environment variables holding its secret; the loader refuses a value that looks like a pasted
  secret instead of a variable name.
- **Nothing stored**: the directory is asked when the page is opened, cached five minutes in
  memory. Sign-in never depends on it.
- **The report** gives each person the role sign-in would give (the first mapped group in
  `sign_in.roles` order, the same function), and warns about a mapped group that does not exist,
  people in several mapped groups, and groups that give no role. Mapping keys match a group by name
  (Keycloak, Okta) or by object ID (Entra tokens carry IDs).
- **Who sees names**: everyone signed in sees the mapping and what each role may see and do; the
  names of people in each group only the roles in `sign_in.admin_roles` (compliance in the clinic
  profile).
- **Pages and commands**: `/access` (linked from the dashboard), and in the terminal
  `python -m auth.directory` or `./scripts/keycloak_setup.sh list`. For the local Keycloak,
  `./scripts/keycloak_setup.sh add <user> <group>` adds a test person.

## Consequences
- An administrator can check a configuration before switching sign-in on: a typo in a group name
  shows as "not found".
- People are still added to groups in the identity provider, never in the pipeline. Provisioning
  them in (SCIM) was considered and left out: it would add a user store to secure, and nothing
  here needs to know a person before they sign in.
- Entra ID: a person in more than about 200 groups gets no groups claim in the token (the
  "overage" case) and so no role. Organizations should send only the groups assigned to the
  application, or use app roles (`groups_claim: roles`).
- Subgroups in Keycloak are not listed yet; neither are Okta groups beyond the first 500.
