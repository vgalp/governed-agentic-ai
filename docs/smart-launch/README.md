# Launching from inside the EHR (ADR 035)

The clinician opens the assistant from a patient's chart. It reads with the clinician's own access,
on that chart, and the record system's audit shows the access under their name.

| | What to register | Launch from |
|---|---|---|
| Epic | A second app at fhir.epic.com: clinician-facing, SMART on FHIR, the R4 read APIs only | Epic's sandbox launch (or the hospital's test Hyperspace) |
| Oracle Health | A provider app in the code console: SMART launch, user scopes | The console's SMART app launcher, secure sandbox |

For both, register:

- **Launch URL:** `http://localhost:8000/smart/launch` (https in any real deployment)
- **Redirect URI:** `http://localhost:8000/smart/callback`
- **Scopes:** `launch openid fhirUser` and `user/<Type>.read` for Patient, MedicationRequest,
  Medication, Encounter, Condition, AllergyIntolerance and Observation. No write access.

Then in `.env`:

```
EPIC_LAUNCH_CLIENT_ID=...
ORACLE_LAUNCH_CLIENT_ID=...
# For a confidential app, uncomment client_secret_ref in fhir.yaml and set the secret, e.g.
# EPIC_LAUNCH_CLIENT_SECRET=...   (or point client_secret_ref at Azure Key Vault / AWS Secrets Manager)
```

Start the platform with the profile (`./gaai start --profile clinic-oracle`), then launch the app from
the vendor's launcher. The chat page opens with "Opened from the EHR as <name> (Clinician), on the
patient's chart". Ask "Give me a brief": no name is needed.

## What it refuses

- a launch from an EHR address not in `launch.issuers`;
- a sign-in that is not finished within 10 minutes, or finished twice;
- an ID token the EHR did not sign for this app and this sign-in;
- a launch without a patient, or by a user type with no role in `launch.roles`;
- a question about another patient (a different birth date): open their chart instead.

## Secrets in a vault

Any `*_ref` in fhir.yaml, and `GAAI_SESSION_KEY_REF` for the session key, can name:

```
env:NAME
file:~/.config/governed-agentic-ai/epic/privatekey.pem        # owner-only, or it is refused
azure-keyvault:https://<vault>.vault.azure.net/secrets/<name>  # uv add azure-identity azure-keyvault-secrets
aws-secretsmanager:<arn or name>                              # uv add boto3
```

The cloud vaults are reached with the machine's own identity (managed identity or `az login`; the
AWS role or profile).
