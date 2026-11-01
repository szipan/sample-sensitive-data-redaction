"""Shared placeholder handler for not-yet-implemented API routes.

The real handlers (`UploadHandler`, `JobStatusHandler`, `DownloadHandler`,
`RulesListHandler`, `RuleOperationSubmitHandler`,
`RuleOperationStatusHandler`) are implemented in later tasks (6, 9, 10).
Until then, every route wired up in `infra/redaction_stack.py`
points at this single placeholder handler so the API Gateway route
definitions can be created and verified now, without blocking on business
logic. Later tasks swap in the real per-route implementation by changing
each Lambda's code/handler only; the route definitions themselves do not
need to change.
"""
import json


def handler(event, context):
    """Return a 501 Not Implemented JSON response for any request.

    Mirrors the error response shape used elsewhere in the API
    (`{"error": {"code": ..., "message": ...}}`) so clients built against
    the placeholder observe a consistent contract once real handlers are
    swapped in.
    """
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "This endpoint is not implemented yet.",
                }
            }
        ),
    }
