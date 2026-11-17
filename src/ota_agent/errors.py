class OTAError(Exception):
    """An expected, safe-to-display application error."""

    status_code = 502
    code = "upstream_error"


class ValidationError(OTAError):
    status_code = 422
    code = "invalid_input"


class NotFoundError(OTAError):
    status_code = 404
    code = "not_found"


class GuardError(OTAError):
    status_code = 403
    code = "operation_blocked"


class ConflictError(OTAError):
    status_code = 409
    code = "state_conflict"


class AuthenticationError(OTAError):
    status_code = 502
    code = "console_authentication_failed"


class UncertainWriteError(OTAError):
    status_code = 409
    code = "write_result_unknown"


class AuditError(OTAError):
    status_code = 503
    code = "audit_unavailable"


class ConfigurationError(OTAError):
    status_code = 503
    code = "configuration_error"
