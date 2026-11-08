# 028: Sign-in with the organization's identity provider

Status: Accepted · Date: 2026-11-08

## Context
The chat API took the person and their role from the request body: the chat page's role menu
chose what a person could see, and the approver of a change was whatever name the page sent. The
November audit lists this first (SC-1, SC-2, SC-4, SC-11): fine for a demonstration on one machine,
impossible in an office, where anyone could pick "clinician". Every clinic, agency and school
already has an identity provider (Entra ID, Okta, Google Workspace, Keycloak), and adopting the
pipeline must stay configuration, not code.

## Decision
A profile's `sign_in` block (profile.yaml) names the provider and maps its groups to the profile's
roles. `auth/sign_in.py` verifies OpenID Connect access tokens, and the API takes the caller from
the token alone:

- **Modes.** `demo` (default): the request says who and which role, as before. `oidc`: every
  request needs `Authorization: Bearer <access token>`. The environment variable
  `GOVERNED_SIGN_IN` overrides the mode, so a profile can be demonstrated either way unedited.
- **Verification.** The provider's discovery document and signing keys (JWKS), cached, fetched
  again when a token names an unknown key (at most once a minute). Checked: signature (RSA, RSA-PSS
  or EC only; never `none` or a shared secret), issuer, audience (`aud` or `azp`), expiry with
  30 s leeway, and the required claims. A remote issuer must be https. Anything that fails, or a
  provider that cannot be reached, gives no access.
- **Roles.** `sign_in.roles` maps groups to roles; the first entry the person holds wins, so the
  administrator orders it. Every mapped role must exist in roles.yaml (checked at load). A person
  in no mapped group has no role, and the pipeline's existing checks refuse role-checked tools.
- **Routes.** With sign-in on: `/chat`, `/v1/chat/completions` (the token replaces API keys),
  approval decisions (the approver comes from the token, so "not the person who asked" compares
  two signed-in people), `/events`, `/approvals` and `/chat/{id}` need a token; an answer is
  readable only by the person who asked; `sign_in.dashboard_roles` can limit the live dashboard.
  `/profile`, `/health`, `/audit/verify` and `/evaluations` hold no personal data and stay open.
- **Pages.** `ui/auth.js` signs people in with the authorization code flow and PKCE (a public
  client: no secret in the page), keeps the token in the tab only (sessionStorage), renews it with
  the refresh token, adds it to the page's API calls, streams `/events` over fetch so the token is
  a header and never in a URL, shows who is signed in, and hides the role menus.
- **Local provider.** `scripts/keycloak_setup.sh` runs Keycloak (compose profile `auth`, port
  8180, this machine only) with a test realm, the public client, a groups claim, the audience
  `governed-api`, four groups and one test user per group, with random passwords kept in
  `.keycloak.env` (not committed).

## Consequences
- An organization adopts sign-in by editing `sign_in` (issuer, audience, client, group names): no
  code. Entra ID sends group IDs or app roles (`groups_claim: roles`); Keycloak's realm roles work
  with `groups_claim: realm_access.roles`.
- Demonstrations on one machine are unchanged unless sign-in is switched on.
- Testing the two-person rule needs two signed-in people at once: a second browser or a private
  window (the provider's session cookie is shared between tabs of one browser).
- Not yet: the governed MCP endpoint still identifies outside assistants by token introspection
  (ADR 015), not by OIDC; SMART on FHIR (an Epic session supplying the clinician and patient);
  per-tenant claims; rate limits per person (SC-10). These are the next milestones.
