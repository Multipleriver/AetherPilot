"""Stable error categories shared by the controller and native worker."""


class HarnessError(Exception):
    def __init__(self, message, *, stage="operation", code="failed", details=None):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.details = details or {}

    def to_dict(self):
        return {
            "category": type(self).__name__,
            "stage": self.stage,
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


class PreflightError(HarnessError):
    pass


class BackendUnavailableError(PreflightError):
    """The optional native backend is not installed or cannot be located.

    This is deliberately separate from a malformed plan and from a native
    operation failure.  Callers can therefore keep using the offline data
    and validation APIs while presenting a stable, machine-readable result
    for operations that require Aether/PyAether.
    """

    def __init__(self, message="Aether/PyAether backend is unavailable", *, details=None,
                 stage="preflight", code="backend_unavailable"):
        super().__init__(message, stage=stage, code=code, details=details)


class SessionError(HarnessError):
    pass


class OperationError(HarnessError):
    pass


class ValidationError(HarnessError):
    pass


class SimulationError(HarnessError):
    pass


class ArtifactError(HarnessError):
    pass
