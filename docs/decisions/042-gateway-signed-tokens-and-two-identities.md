# 042: Signed tokens at the gateway, and two identities in the audit

Status: Accepted · Date: 2026-11-17 · Tested with a stand-in identity provider and over real connections; not tested against a hospital's provider

## Context
- The governed gateway knew a caller only by asking an identity system about each token (ADR 015). That
  needs a token-check endpoint, which some providers do not offer (Microsoft Entra ID), and it is not
  the sign-in the MCP specification uses (OAuth 2.1 and OpenID Connect, with access granted centrally
  by the identity provider).
- A review of connectors on the market found the same weakness in most: one shared credential, so the
  record of what happened names an application and not a person.
- Turning a trained classifier on for a trial meant editing a committed profile, and tests then
  depended on a developer's local edit. That happened three times.

## Decision
- **A second caller check: `auth.kind: jwt`** in outside.yaml. The gateway verifies each access token
  itself: signature against the provider's published keys, issuer, audience, expiry. It is the same
  verifier as the chat application's sign-in (`auth/sign_in.py`, ADR 028), reused, with the same rule
  that only asymmetric signatures are accepted.
  - The role comes from the token's groups through `auth.roles`, in the administrator's order. A caller
    in no mapped group has no role.
  - Which claims hold the software, the person, the groups and the tenant is configuration, because
    providers differ.
  - A token that does not verify is no caller. The reason is logged; the token never is.
  - The token check (`introspection`) stays, for providers that have one and for revocation within
    seconds. A profile chooses one.
- **Every audit event names two identities:** `client`, the software that called, and `on_behalf_of`,
  the person it acted for, when the identity system gives one. Software acting for itself has no
  person, and none is invented. The token check may return `on_behalf_of` too.
- **`GOVERNED_TEXT_MODEL` and `GOVERNED_TEXT_MODEL_MODE`** turn a sensitive-category classifier on for a
  trial and set what it may do. Committed profiles name no model. The test setup clears these and the
  other trial settings, so tests read the profiles as committed.

## Consequences
- **A signed token is trusted until it expires.** Revocation is as fast as the provider's token
  lifetime. An organization that needs faster uses the token check, or short lifetimes.
- **The audit now holds a person's identifier.** That is what an audit trail is for, and it is the
  provider's identifier for a member of staff, not patient data. A deployment should use an opaque
  subject, not an email address, where its policy asks for that.
- **Enterprise-managed authorization is the identity provider's side.** With signed tokens accepted,
  the provider can grant access centrally. The discovery document that lets a client find the
  provider from the gateway is not served yet.
- **The rate limit is still per software client,** not per person.
- **Not tested:** a real provider (Entra ID, Okta, Keycloak) issuing the tokens. The repository's
  Keycloak script is the next step.
