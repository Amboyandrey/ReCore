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


class MemberNotFound(AppError):
    """Raised when the targeted user isn't a member of the workspace."""

    status_code = 404
    detail = "Member not found."


class InvitationInvalid(AppError):
    """Raised for an invite token that's unknown, expired, already accepted, or wrong-email."""

    status_code = 400
    detail = "This invitation is invalid or has expired."


class InvitationNotFound(AppError):
    """Raised when revoking an invitation id that doesn't exist in the given workspace."""

    status_code = 404
    detail = "Invitation not found."


class LastOwnerError(AppError):
    """Raised when an action would leave a workspace with no owner at all."""

    status_code = 409
    detail = "A workspace must always have at least one owner."


class CredentialNotFound(AppError):
    """Raised for a provider credential that doesn't exist in the workspace."""

    status_code = 404
    detail = "Credential not found."


class CredentialValidationFailed(AppError):
    """Raised when a provider rejects a key at registration time — it's never stored unvalidated."""

    status_code = 422
    detail = "The provider rejected this key."


class ModelNotFound(AppError):
    """Raised for an enabled model that doesn't exist in the workspace."""

    status_code = 404
    detail = "Model not found."


class ConversationNotFound(AppError):
    """Raised for a conversation that doesn't exist in the workspace."""

    status_code = 404
    detail = "Conversation not found."


class GenerationNotFound(AppError):
    """Raised when trying to resume or stop a generation that isn't running (or never existed)."""

    status_code = 404
    detail = "Generation not found."


class SuperuserRequired(AppError):
    """Raised when a non-superuser calls an admin-only route (flag and global user management)."""

    status_code = 403
    detail = "This action requires superuser access."


class FlagNotFound(AppError):
    """Raised for a flag key that has no definition."""

    status_code = 404
    detail = "Flag not found."


class FlagKeyAlreadyExists(AppError):
    """Raised when creating a flag whose key is already taken — keys are the stable identifier."""

    status_code = 409
    detail = "A flag with this key already exists."


class FeatureDisabled(AppError):
    """Raised by a route gated with `flag_gate()` when the flag resolves to off for this caller."""

    status_code = 404
    detail = "This feature isn't available."


class ProviderDisabled(AppError):
    """Raised when sending a message whose model's provider has been switched off by an admin.

    Existing conversations and their history stay readable — only starting a new generation
    through that provider is blocked.
    """

    status_code = 403
    detail = "This provider has been disabled by an administrator."


class AttachmentNotFound(AppError):
    """Raised for an attachment that doesn't exist in the conversation it was looked up under."""

    status_code = 404
    detail = "Attachment not found."


class AttachmentTooLarge(AppError):
    """Raised when an uploaded file exceeds the configured size limit."""

    status_code = 413
    detail = "This file is too large."


class ModelDoesNotSupportImages(AppError):
    """Raised pre-flight when an image is attached to a message but the conversation's model
    isn't marked `supports_vision` — caught before anything is persisted or a generation starts,
    rather than letting the image silently vanish or the provider reject the request mid-stream.
    """

    status_code = 422
    detail = "This model can't read images. Pick a vision-capable model or remove the image."


class InvalidCursor(AppError):
    """Raised when a cursor-paginated list gets a `before`/`cursor` value it can't parse."""

    status_code = 400
    detail = "Invalid pagination cursor."


class ToolNotFound(AppError):
    """Raised for a tool id that doesn't exist in the workspace it was looked up under."""

    status_code = 404
    detail = "Tool not found."


class ToolNameAlreadyExists(AppError):
    """Raised when registering a tool whose name is already taken in this workspace — the model
    sees this name as the function it's calling, so two tools can't share one."""

    status_code = 409
    detail = "A tool with this name already exists."


class AssistantNotFound(AppError):
    """Raised for an assistant id that doesn't exist in the workspace it was looked up under."""

    status_code = 404
    detail = "Assistant not found."
