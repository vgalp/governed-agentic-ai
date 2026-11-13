"""The errors a FHIR call can end in (ADR 032, ADR 036).

Every one is a `FhirError`, so callers that catch `FhirError` (the record tools, which turn it into
"the record system did not answer ...") keep working. The subclasses let a caller, a metric or a
test tell the reasons apart. Messages are shown to staff: they name the server's host and the
resource type, never a patient id or a token.
"""


class FhirError(Exception):
    """The FHIR or authorization server refused or failed a request."""


class FhirResponseError(FhirError):
    """The server answered, but not with what was asked for (malformed or unexpected)."""


class FhirUnavailableError(FhirError):
    """The server is not being called: its circuit breaker is open, or it cannot be reached."""


class FhirDeadlineError(FhirError):
    """The time budget for the question ran out before the records could be read."""
