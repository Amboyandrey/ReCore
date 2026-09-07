"""Domain exceptions — services raise these, one handler in main.py turns them into responses."""


class AppError(Exception):
    """Base for exceptions that map to a specific HTTP response instead of a raw 500."""

    status_code: int = 500
    detail: str = "Something went wrong."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        if detail is not None:
            self.detail = detail


class EmailAlreadyRegistered(AppError):
    """Raised on signup when the email is already tied to an account."""

    status_code = 409
    detail = "An account with this email already exists."


class InvalidCredentials(AppError):
    """Raised on login for any reason — unknown email or wrong password give the same message."""

    status_code = 401
    detail = "Invalid email or password."


class SessionInvalid(AppError):
    """Raised when a request's session cookie is missing, expired, or points at a deleted user."""

    status_code = 401
    detail = "Your session has expired. Please sign in again."


class CsrfTokenInvalid(AppError):
    """Raised when a mutating request's CSRF header is missing or doesn't match the session."""

    status_code = 403
    detail = "Invalid CSRF token."


class RateLimited(AppError):
    """Raised when a caller has exceeded the allowed rate for a rate-limited action."""

    status_code = 429
    detail = "Too many attempts. Please try again later."


class WorkspaceNotFound(AppError):
    """Raised for a workspace that doesn't exist, is deleted, or the caller isn't a member of.

    404, not 403 — telling a non-member a workspace exists would itself leak information.
    """

    status_code = 404
    detail = "Workspace not found."


class InsufficientRole(AppError):
    """Raised when the caller's role in the workspace doesn't meet what the action requires."""

    status_code = 403
    detail = "You don't have permission to do that."


class InvitationInvalid(AppError):
    """Raised for an invite token that's unknown, expired, already accepted, or wrong-email."""

    status_code = 400
    detail = "This invitation is invalid or has expired."


class LastOwnerError(AppError):
    """Raised when an action would leave a workspace with no owner at all."""

    status_code = 409
    detail = "A workspace must always have at least one owner."
