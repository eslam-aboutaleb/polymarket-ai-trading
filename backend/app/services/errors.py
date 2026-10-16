"""Domain error taxonomy — why a request was refused, without the transport.

Services are imported by more than routers: schedulers, background
workers, CLI tools and tests all call ``EntitlementService`` and
``normalize_scopes``. Raising ``fastapi.HTTPException`` from a service
forces every one of those callers to be an HTTP endpoint — a job that
hits a paywall must either import FastAPI or crash, and a test must
reach into the service's HTTP details just to find out what went wrong.
A refusal is therefore a :class:`DomainError`: a ``code`` for the client,
a ``message`` for a human, and the structured fields the caller needs,
with no transport attached. The API layer is what turns one into a
response, in one place (``app.api.deps._http_error``, via
:func:`to_http`).

Invariants
----------

* **``to_http`` reproduces today's bytes.** The status, body and
  ``Retry-After`` header each error produces are the ones the services
  built inline before this taxonomy existed. Migrating a service
  changes *who raises*, never what a client sees, which is what makes
  the change reviewable one service at a time.
* **Codes are a public contract.** Clients (the React UI included)
  branch on ``code``, so a code is never renamed or removed — a new
  refusal is a new class with a new code.
* **The classes remain ``HTTPException`` subclasses, for now.** Call
  sites that have not been translated yet — most route modules, and the
  current test suite — still catch ``HTTPException``, so the error a
  service raises *is* one, projected from the same ``status_code`` /
  ``body`` / ``http_headers`` that :func:`to_http` reads. New code
  catches :class:`DomainError` and translates at its own boundary; the
  bridge (``_HttpBridge``) is deleted when the last inline
  ``HTTPException`` caller is gone.
* **One body shape per failure.** Where the legacy response carried a
  plain sentence rather than the structured ``{code, message}`` payload
  (``ApiKeyInvalidError``, ``AccountAccessDeniedError``), the taxonomy
  keeps the sentence: byte-identical beats tidier.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

#: Where every plan refusal sends the user. One constant so the
#: paywall and the account-limit error cannot drift apart.
UPGRADE_URL = "/settings?tab=billing"


class DomainError(Exception):
    """Base class for every refusal a service can make.

    Carries the machine-readable ``code``, the human ``message``, and
    the HTTP projection (``status_code``, :attr:`body`,
    :attr:`http_headers`) that :func:`to_http` renders. Nothing else in
    the service layer needs to know a refusal was ever a 402.
    """

    #: Stable, machine-readable code. Clients branch on it.
    code: str = "request_refused"
    #: Human-readable default; a subclass overrides the class attribute
    #: and a caller may pass a more specific ``message``.
    message: str = "The request was refused."
    #: The HTTP status this refusal maps to (see :func:`to_http`).
    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str | None = None) -> None:
        self.message = str(message or self.message)
        Exception.__init__(self, self.message)

    @property
    def body(self) -> Any:
        """The response body this refusal produces.

        Named ``body`` rather than ``detail`` on purpose:
        ``HTTPException.__init__`` assigns ``self.detail``, and a
        read-only property of that name would collide with it.
        """
        return {"code": self.code, "message": self.message}

    @property
    def http_headers(self) -> dict[str, str] | None:
        """Response headers, for the refusals that need one."""
        return None

    def http_kwargs(self) -> dict[str, Any]:
        """Exactly what ``fastapi.HTTPException`` needs to render :attr:`body`."""
        kwargs: dict[str, Any] = {"detail": self.body}
        if self.http_headers is not None:
            kwargs["headers"] = self.http_headers
        return kwargs


class _HttpBridge(HTTPException):
    """Renders a :class:`DomainError` as the ``HTTPException`` it replaced.

    A migration bridge and deliberately the only FastAPI-aware piece of
    the taxonomy: callers that have not been translated yet still catch
    ``HTTPException``, and the projection is computed from the same
    fields :func:`to_http` reads, so the two paths cannot drift.

    ``DomainError.__init__`` and ``HTTPException.__init__`` are invoked
    explicitly rather than cooperatively: the MRO chain would reach
    ``HTTPException.__init__(message)``, whose first parameter is
    ``status_code``.
    """

    def __init__(self, message: str | None = None) -> None:
        DomainError.__init__(self, message)
        HTTPException.__init__(self, status_code=self.status_code, **self.http_kwargs())


class EntitlementDenied(DomainError, _HttpBridge):
    """402: the account's plan does not include ``capability``.

    The body is structured so the frontend renders an upgrade CTA from
    it without a second lookup, and it carries a plain ``message`` so
    the shared ``getApiErrorMessage`` helper shows something readable.
    """

    code = "plan_required"
    status_code = status.HTTP_402_PAYMENT_REQUIRED

    def __init__(
        self,
        *,
        capability: str,
        current_plan: str,
        required_plan: str,
        message: str,
    ) -> None:
        self.capability = capability
        self.current_plan = current_plan
        self.required_plan = required_plan
        _HttpBridge.__init__(self, message)

    @property
    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "capability": self.capability,
            "current_plan": self.current_plan,
            "required_plan": self.required_plan,
            "upgrade_url": UPGRADE_URL,
        }


class CapabilityUnknown(DomainError, _HttpBridge):
    """403: ``capability`` is not one any plan grants.

    Deliberately not an upgrade hint — pointing a user at a paid tier
    that does not have the capability either would be a lie — and the
    same shape answers "this capability has no quota on any plan" from
    the quota path.
    """

    code = "capability_unknown"
    status_code = status.HTTP_403_FORBIDDEN

    def __init__(self, capability: str, message: str | None = None) -> None:
        self.capability = capability
        _HttpBridge.__init__(
            self,
            message or f"'{capability}' is not an available capability.",
        )

    @property
    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "capability": self.capability,
        }


class AccountLimitExceeded(DomainError, _HttpBridge):
    """402: the owner already holds every account the plan allows.

    The ceiling is per owner and the plan that applies is the owner's
    personal one, so the counts and the plan in this body are the
    owner's, exactly as the inline 402 reported them.
    """

    code = "account_limit"
    status_code = status.HTTP_402_PAYMENT_REQUIRED

    def __init__(self, *, current_plan: str, limit: int, used: int, message: str) -> None:
        self.current_plan = current_plan
        self.limit = limit
        self.used = used
        _HttpBridge.__init__(self, message)

    @property
    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "current_plan": self.current_plan,
            "limit": self.limit,
            "used": self.used,
            "upgrade_url": UPGRADE_URL,
        }


class QuotaExceeded(DomainError, _HttpBridge):
    """429: the daily budget for ``capability`` is spent.

    A quota is a cost control, never a safety mechanism, so the body
    points at the reset rather than suggesting the request is invalid.
    """

    code = "quota_exceeded"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, *, capability: str, limit: int, retry_after_seconds: int) -> None:
        self.capability = capability
        self.limit = limit
        # Clamped to one second: a zero would tell a well-behaved
        # client to retry immediately against a budget that resets at
        # midnight.
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        _HttpBridge.__init__(
            self,
            f"Daily limit reached for {capability.replace('_', ' ')} ({limit}). "
            "The budget resets at 00:00 UTC; upgrade for a higher limit.",
        )

    @property
    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "capability": self.capability,
            "limit": self.limit,
        }

    @property
    def http_headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after_seconds)}


class ApiKeyInvalidError(DomainError, ValueError, _HttpBridge):
    """400: a requested API-key configuration is not valid.

    Also a ``ValueError`` so a caller that guards ``normalize_scopes``
    with ``except ValueError`` — the shape a non-HTTP caller naturally
    writes — keeps working.
    """

    code = "api_key_invalid"
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str) -> None:
        _HttpBridge.__init__(self, message)

    @property
    def body(self) -> str:
        """The plain sentence the API-key endpoints have always returned.

        Not the structured ``{code, message}`` payload the plan errors
        carry: an unknown scope was answered with this sentence before
        the taxonomy existed, and ``code`` stays on the object for the
        callers that branch on it.
        """
        return self.message


class AccountAccessDeniedError(DomainError, _HttpBridge):
    """403: the account exists but belongs to a different user.

    The taxonomy entry for the failure
    :class:`~app.services.users.account_service.AccountAccessDenied`
    raises today, so the API layer has one name to translate and one
    body to keep stable while that service migrates.
    """

    code = "account_access_denied"
    status_code = status.HTTP_403_FORBIDDEN
    message = "Account not found or not owned by caller"

    def __init__(self, message: str | None = None) -> None:
        _HttpBridge.__init__(self, message)

    @property
    def body(self) -> str:
        """A plain sentence, as the guard has always returned it."""
        return self.message


def to_http(exc: DomainError) -> tuple[int, dict[str, Any]]:
    """Project a domain error onto ``(status_code, HTTPException kwargs)``.

    The second element is exactly what ``fastapi.HTTPException`` takes
    — ``detail`` always, ``headers`` when the error carries one
    (``QuotaExceeded``'s ``Retry-After``) — so an API caller translates
    with::

        status_code, kwargs = to_http(exc)
        raise HTTPException(status_code=status_code, **kwargs)

    and the response is byte-identical to the inline exception the
    service raised before the taxonomy existed.
    """
    return exc.status_code, exc.http_kwargs()
