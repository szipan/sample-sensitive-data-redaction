"""The Logging_Configurator (agent-log-delivery design.md "Component 1:
`backend/agent/logging_config.py`").

The agent writes 15 log statements and delivers none of them: every module
under `backend/agent/` calls `logging.getLogger(__name__)`, nothing ever
installs a handler, and the AgentCore Runtime container — unlike the AWS
Lambda Python runtime — has no convention that installs one for us. Records
therefore propagate to a handler-less root logger and are dropped. This
module is the one place that installs a handler, sets the levels, and
decides what a record is allowed to carry.

Standard library only. No AWS Lambda Powertools, no third
party: this module is imported at `agent` package import time, so it must not
be able to fail on a missing dependency, and matching the AgentCore SDK's
JSON field set costs one `format` method.

`configure_logging` is the entry point; everything else here is a piece it
composes.
"""

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

#: The dotted-name root components this project owns. Everything else —
#: `botocore`, `boto3`, `urllib3`, `httpx`, `opentelemetry`, `strands`,
#: `bedrock_agentcore` — is a Third_Party_Logger. Used as an allow-list in
#: two places (the handler filter and `describe_exception`), never as a
#: deny-list of third-party names: an allow-list is closed by construction,
#: whereas a deny-list fails open the first time a dependency is added.
OWNED_LOGGER_ROOTS = frozenset({"agent", "shared"})

#: The environment variable `CDK_Stack` sets alongside `GUARDRAIL_ID` and
#: `PDF_OCR_DPI`.
LOG_LEVEL_ENV_VAR = "LOG_LEVEL"

#: `INFO` discards the digit-boundary filter's "kept N of M"
#: DEBUG line on a healthy run; `DEBUG` by default would instead turn on
#: per-detection records for every unit of every document, which is a volume
#: and content risk in exchange for a diagnostic nobody reads.
DEFAULT_LOG_LEVEL = logging.INFO

#: Explicit, deliberately *not* `logging.getLevelNamesMapping()` and
#: deliberately not `logging.getLevelName()`. The explicit form refuses
#: `NOTSET` (which would mean "inherit", not a threshold) and refuses numeric
#: strings like `"10"` — both are misconfigurations rather than intentions,
#: and both are what an implementation built on the stdlib lookups gets
#: wrong, in opposite directions. Enumerating the five names makes
#: "names no Python logging level" a closed question
#: instead of a question about stdlib internals.
_LEVEL_NAMES = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

#: Attribute stamped on the handler this module installs, so a second
#: `configure_logging()` call can find and reuse it. An
#: attribute marker rather than a module-level `_configured` flag: a flag
#: would make the second call a no-op, where what is wanted is re-applying
#: levels without adding a second handler.
_HANDLER_MARKER = "_agent_log_delivery_handler"

#: What `describe_exception` returns for an exception type nobody has
#: audited. See that function's docstring for why omission is the default.
_OMITTED_MESSAGE = "<message omitted: unaudited exception type>"


def resolve_log_level(raw: Optional[str]) -> Tuple[int, Optional[str]]:
    """Resolve the Effective_Log_Level from a raw `LOG_LEVEL` string.

    Returns `(level, warning_or_None)`. Absent, whitespace-only, unknown and
    wrong-case inputs are all covered by comparing `raw.strip().upper()`
    against `_LEVEL_NAMES`. Total by
    construction: every branch returns a level, so no value of `LOG_LEVEL`
    can make the Agent_Runtime_Process fail at import.

    The warning is **returned rather than emitted**. At the
    point the level is resolved no handler exists yet — resolving the level
    is step one of installing one — so emitting here would discard the very
    record that reports the misconfiguration. Emitting it after installation
    is the only ordering that satisfies 4.4, and it makes the warning the
    first record in the stream, visible exactly when an operator has just
    changed the variable.

    A warning is returned for a present, non-blank, unrecognised value only.
    Absent and blank are the documented way to ask for the default,
    not mistakes worth a record.
    """
    if raw is None:
        return DEFAULT_LOG_LEVEL, None

    candidate = raw.strip().upper()
    if not candidate:
        return DEFAULT_LOG_LEVEL, None

    level = _LEVEL_NAMES.get(candidate)
    if level is not None:
        return level, None

    return (
        DEFAULT_LOG_LEVEL,
        "{} is set to an unrecognized value; applying {} instead".format(
            LOG_LEVEL_ENV_VAR, logging.getLevelName(DEFAULT_LOG_LEVEL)
        ),
    )


def describe_exception(error: BaseException) -> str:
    """Render a caught exception's message for a Log_Record, sanitized.

    This is the only place in the feature that consults `str(exc)`, and it is
    what closes the exception-message leak channel.

    The hazard is concrete, not hypothetical. `ApplyGuardrail`'s request body
    *is* the document text (`content[0].text.text`) and
    `DetectDocumentText`'s *is* the rasterized page image, so a `botocore`
    `ParamValidationError` quoting the offending parameter can echo a whole
    document back through `str(exc)`. Two call sites interpolate a caught
    error into their message, and `JsonLogFormatter`'s `errorMessage` field
    renders one for every record carrying `exc_info` — all of them go
    through here.

    Three branches, in order:

    1. **The exception is ours.** If the exception type's own module root is
       in `OWNED_LOGGER_ROOTS`, `str(error)` is returned. These messages are
       audited by construction: `OcrPathError`, `OrchestrationError` and
       `InvalidInvocationPayloadError` interpolate a page index, a jobId, a
       dpi value or a payload field name — all Safe_References.
    2. **The exception is an AWS client error.** The `botocore`
       `ClientError.response` shape is read duck-typed via `getattr`, the
       same way `guardrails_pii_detect._is_retryable` reads it, so this
       module stays importable — and unit-testable with a plain fake — with
       no `botocore` installed. The error *code* is the diagnostically
       load-bearing part (`ThrottlingException` vs `ValidationException` vs
       `AccessDeniedException`); the free text around it is discarded.
    3. **Anything else is omitted.**

    Why omission is the default rather than the exception: an exception type
    nobody has audited should be silent rather than leak. Nothing has ever
    been delivered from this process, so nothing has ever been read — the
    safe assumption about an unfamiliar type's `str()` is that it may quote
    its input. The cost of being wrong in the other direction is bounded:
    `errorType` is always present on the record, so an omitted message still
    leaves a type name to search on, plus a `stackTrace` of frames.
    """
    module = type(error).__module__ or ""
    if module.split(".", 1)[0] in OWNED_LOGGER_ROOTS:
        return str(error)

    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return "AWS error code {}".format(code)

    return _OMITTED_MESSAGE


class OwnedLoggerFilter(logging.Filter):
    """Admit every record from an Application_Logger, and only INFO-and-above
    from anything else.

    One expression, and the shape of it is the point:

        record.name.split(".", 1)[0] in OWNED_LOGGER_ROOTS
            or record.levelno >= logging.INFO

    **Unconditional.** It consults neither `LOG_LEVEL` nor any other
    environment variable, so no configuration can turn it off. That matters
    because `LOG_LEVEL=DEBUG` is exactly what an operator sets while
    debugging a job, and under a deny-list-free root handler that is the one
    action that would otherwise open the leak: `botocore.endpoint` logs
    `"Request body: %s"` at DEBUG, and the `ApplyGuardrail` request body *is*
    the document text while the `DetectDocumentText` body *is* the rasterized
    page image.

    **An allow-list, not a deny-list.** No third-party logger name is
    enumerated here or anywhere else in this module — not `botocore`, not
    `boto3`, not `urllib3`. A deny-list of names fails open the first time a
    dependency is added, because the new package's name is not on it. An
    allow-list is closed by construction: a logger this project does not own
    is held at the INFO floor whether or not anyone anticipated it.

    This is the **second** of the two mechanisms that hold the level at the INFO floor.
    The first is `configure_logging`'s `root.setLevel(max(level, INFO))`
    floor, which is strictly better where it applies — a record held out by
    the root level is never even constructed, so the document text is never
    interpolated into a string. The floor cannot reach a library that sets
    its own level in code, though: `boto3.set_stream_logger()`, or a package
    calling `logger.setLevel(logging.DEBUG)` at import, gives its logger an
    explicit level that beats the root's, and the record then arrives at the
    handler regardless of the floor. This filter is what stops it there.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if `record` may be written."""
        return (
            record.name.split(".", 1)[0] in OWNED_LOGGER_ROOTS
            or record.levelno >= logging.INFO
        )


class JsonLogFormatter(logging.Formatter):
    """Render a Log_Record as one line of JSON.

    The field names mirror `bedrock_agentcore.runtime.app.RequestContextFormatter`,
    read out of the installed SDK rather than guessed:
    `timestamp`, `level`, `message`, `logger`, `location`, and — when the
    record carries exception information — `errorType`, `errorMessage`,
    `stackTrace`. Matching the SDK matters because both formatters write into
    the same CloudWatch group, and a Logs Insights query should not need two
    spellings of the same concept.

    Two fields the SDK emits are **deliberately absent**: `requestId` and
    `sessionId`. The SDK reads them from `BedrockAgentCoreContext`, and
    importing that would break the standard-library-only constraint
    for a module that is imported at `agent` package import
    time. Their absence doubles as the way a reader tells an application
    record from an SDK record in a mixed stream.

    Two rendering choices are load-bearing rather than cosmetic:

    - `ensure_ascii=False`. A future statement carrying a rule name in
      Chinese would otherwise arrive as unreadable `\\uXXXX` escapes.
    - `default=str`. Every value this formatter builds is
      already a `str` or a `list[str]`, so nothing here needs it today; it is
      the guarantee that an unserializable value added later degrades to its
      string representation instead of raising inside `json.dumps` and
      costing the whole record.
    """

    #: The SDK renders `%Y-%m-%dT%H:%M:%S.%f` truncated to milliseconds plus a
    #: literal `Z`. Same output here, from an aware UTC clock rather than the
    #: SDK's deprecated `datetime.utcnow()`.
    _TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

    def format(self, record: logging.LogRecord) -> str:
        """Return `record` as a single-line JSON object."""
        message, format_error = self._render_message(record)

        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime(self._TIMESTAMP_FORMAT)[:-3]
            + "Z",
            "level": record.levelname,
            "message": message,
            "logger": record.name,
            "location": "{}:{}:{}".format(
                record.pathname, record.funcName, record.lineno
            ),
        }

        if format_error is not None:
            entry["formatError"] = format_error

        exc_info = record.exc_info
        # `logging` normalizes `exc_info=True` into a 3-tuple before the record
        # reaches a handler, but a hand-built LogRecord can carry the raw
        # `True`. Checked rather than unpacked blindly: an exception raised
        # here would be swallowed by `logging` and the record dropped, and
        # this feature exists to stop records being dropped.
        if exc_info and isinstance(exc_info, tuple) and len(exc_info) == 3:
            exc_type, exc_value, exc_traceback = exc_info

            if exc_value is not None:
                entry["errorType"] = type(exc_value).__name__
                # The only place in this feature that `str(exc)` is ever
                # consulted, and it is consulted behind the audit in
                # `describe_exception`.
                entry["errorMessage"] = describe_exception(exc_value)
            elif exc_type is not None:
                entry["errorType"] = getattr(exc_type, "__name__", str(exc_type))

            # `format_tb`, not `format_exception`, and this is the whole reason
            # the field exists in this shape. `format_exception`'s last line is
            # `f"{type}: {str(exc)}"` — and `str(exc)` on a botocore
            # `ParamValidationError` can quote the offending parameter, which
            # for `ApplyGuardrail` is the document text and for
            # `DetectDocumentText` is the page image. The SDK's own formatter
            # does exactly that; this one does not. `format_tb` also never
            # walks `__cause__` / `__context__`, so
            # `raise OcrPathError(...) from botocore_error` cannot smuggle the
            # botocore message in through the chain. What is left is frames:
            # file, line number, function name and source line text — code,
            # never a value.
            entry["stackTrace"] = traceback.format_tb(exc_traceback)

        return json.dumps(entry, ensure_ascii=False, default=str)

    @staticmethod
    def _render_message(record: logging.LogRecord) -> Tuple[str, Optional[str]]:
        """Interpolate `record`'s message, guarded.

        Returns `(message, format_error_or_None)`.

        `record.getMessage()` raises when the format string and the args
        disagree — a `%s` count mismatch, a `%d` against a string. `logging`
        swallows a formatter exception and **drops the record**, which is the
        worst outcome available to a feature about records not being dropped:
        the diagnostic and the fact that a diagnostic was attempted both
        vanish.

        On failure the message becomes `str(record.msg)`, which is a literal
        at all 22 call sites, and a `formatError` field carries the
        interpolation failure's type name. **Never `record.args`** — that is
        where a Sensitive_Value would be, and it is precisely the value that
        failed to be safely interpolated.
        """
        try:
            return record.getMessage(), None
        except Exception as interpolation_error:  # noqa: BLE001 - never re-raise
            format_error = type(interpolation_error).__name__
            try:
                return str(record.msg), format_error
            except Exception:  # noqa: BLE001 - a __str__ that raises, too
                return "<message unavailable>", format_error


def configure_logging() -> logging.Handler:
    """Install log delivery for the Agent_Runtime_Process and return the handler.

    This is the Logging_Configurator's entry point.
    Eight steps, in the order below; several of them are load-bearing for a
    reason that is not obvious from the code, so each carries its own note.

    **It must not be able to raise.** It runs at `agent` package import,
    so an exception here takes the whole process down before a
    single job is dispatched. Nothing here can: the module imports only the
    standard library, so it cannot fail on a missing dependency; it reads
    exactly one environment variable; and every branch of
    `resolve_log_level` returns a level, so no value of `LOG_LEVEL` —
    including a hostile one — produces anything but a threshold plus an
    optional warning string. Keep it that way: any future step added here
    must be total in the same sense.

    **It is idempotent**: calling it twice
    installs one handler, not two, and re-applies levels both times.
    """
    # --- Step 1: resolve the level ------------------------------------------
    # The warning is returned, not emitted. No handler exists yet, so a record
    # emitted here would be discarded — which is why step 7 exists.
    level, level_warning = resolve_log_level(os.environ.get(LOG_LEVEL_ENV_VAR))

    root = logging.getLogger()

    # --- Step 2: find or create the single marked handler -------------------
    # Idempotence is keyed on an attribute stamped on the handler, *not* on a
    # module-level `_configured` flag. A flag would make the second call a
    # no-op; what is wanted is a second call that re-applies levels without
    # adding a second handler. That distinction matters because there are two
    # callers by design: `agent/__init__.py` configures at package import
    # and the tracked entrypoint calls it explicitly,
    # and whichever runs second must still take a changed `LOG_LEVEL` into
    # account. Without this, a duplicate handler would also break
    # the exactly-one-record guarantee.
    handler: Optional[logging.Handler] = None
    for existing in root.handlers:
        if getattr(existing, _HANDLER_MARKER, False):
            handler = existing
            break

    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)

    # Re-applied on every call rather than only at creation, so a handler
    # whose filter or formatter was swapped out (by a test, or by a foreign
    # component) is restored. `filters` is reset rather than appended to,
    # since `addFilter` on a reused handler would stack duplicates.
    handler.filters = [OwnedLoggerFilter()]
    handler.setFormatter(JsonLogFormatter())
    # The handler admits everything it is given; the level decisions live on
    # the loggers (steps 3-5) and in the filter, where a suppressed record is
    # never constructed rather than merely discarded.
    handler.setLevel(logging.NOTSET)

    # --- Step 3: the root level is a FLOOR, not the configured level --------
    # This single line is the primary enforcement of the INFO floor, and the
    # one an operator cannot widen through configuration.
    #
    # Every third-party logger in the bundle — `botocore`, `boto3`, `urllib3`,
    # `httpx`, `opentelemetry`, `strands` — has no explicit level of its own,
    # so all of them inherit this floor. The consequence that matters:
    # `botocore.endpoint`'s `logger.debug("Request body: %s", body)` is never
    # *constructed*. Not filtered at the handler — never built. The
    # `ApplyGuardrail` request body *is* the document text and the
    # `DetectDocumentText` body *is* the rasterized page image, so a record
    # that is never interpolated into a string is strictly better than one
    # interpolated and then rejected downstream.
    #
    # `LOG_LEVEL=DEBUG` lowers `agent` and `shared` (steps 4-5) and leaves
    # this floor at INFO. No configuration widens it. `OwnedLoggerFilter` is
    # the backstop for the one case the floor cannot reach — a library that
    # gives its own logger an explicit level in code, which then beats the
    # root's.
    root.setLevel(max(level, logging.INFO))

    # --- Step 4: the Effective_Log_Level on the owned roots -----------------
    for owned_root in OWNED_LOGGER_ROOTS:
        logging.getLogger(owned_root).setLevel(level)

    # --- Step 5: sweep already-created descendants of the owned roots -------
    # A child logger's explicit level beats an ancestor's, so
    # step 4 alone is not enough: `agent/runtime.py` calls
    # `logger.setLevel(logging.INFO)` on its own module logger today, which
    # under `LOG_LEVEL=DEBUG` would pin that module at INFO no matter what
    # `agent` is set to.
    #
    # The sweep only reaches loggers that already exist when configuration
    # runs. A module imported *afterwards* that sets its own level is out of
    # reach — which is exactly why that `setLevel` line is deleted
    # instead of relying on the sweep alone. The sweep is the safety net for
    # import orders we do not control; removing the call is the fix.
    #
    # Iterated over a snapshot: `getLogger` mutates `loggerDict`. `PlaceHolder`
    # entries are skipped rather than materialized — a placeholder means no
    # logger of that name exists yet, so it carries no explicit level to
    # override and will inherit from step 4 when something creates it.
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if not isinstance(obj, logging.Logger):
            continue
        if name.split(".", 1)[0] in OWNED_LOGGER_ROOTS:
            obj.setLevel(level)

    # --- Step 6: stop the SDK logger double-emitting ------------------------
    # This keeps the exactly-one-record guarantee. `bedrock_agentcore.app` already
    # carries its own StreamHandler and formatter, installed by the SDK, and
    # propagates to root by default. Without this line the 4 events per job
    # that reach CloudWatch today would become 8 — once through the SDK's
    # handler, once through ours.
    logging.getLogger("bedrock_agentcore.app").propagate = False

    # --- Step 7: emit the deferred warning, now that a handler exists -------
    # This ordering is the only one that works: the level
    # was resolved in step 1, before any handler existed, so a record emitted
    # there would have been dropped by the very defect this module fixes.
    # Emitting here also makes the misconfiguration warning the first record
    # in the stream — visible exactly when an operator has just changed the
    # variable.
    if level_warning is not None:
        logging.getLogger(__name__).warning("%s", level_warning)

    # --- Step 8: hand the handler back -------------------------------------
    # Returned so tests can drive the filter and the formatter directly, which
    # is what the tests' capture fixture needs: it redirects this handler's
    # stream to an in-memory buffer and asserts on the serialized output.
    # `caplog` cannot substitute — it attaches its own unfiltered, unformatted
    # handler, so it would bypass both of the things under test.
    return handler
