"""`poll_for_confirmation` — waits for user confirmation of detection results.

After the Agent sets a review-mode job's status to ``"detected"`` and
persists ``detections.json`` to S3, it enters a polling loop reading the
``RedactionJobs`` DynamoDB record every few seconds until the user confirms
which Detection_Values to redact (via the ``RedactHandler`` Lambda writing
``confirmedSelections`` to the record) or the timeout expires.

If confirmation arrives within the timeout window, the function returns
the list of selected Detection_Value IDs. If the timeout expires without
confirmation, it returns ``None`` — signaling the caller to redact ALL
detected values.

The AgentCore SDK keeps the session alive via a HealthyBusy ping while
``time.sleep`` blocks, so the Agent will not be terminated during the wait.

Requirements: 4.1, 4.2, 4.4
"""

import logging
import os
import time
from typing import List, Optional

import boto3

logger = logging.getLogger(__name__)

# Poll every 5 seconds.
POLL_INTERVAL_SECONDS = 5

# Maximum wait time: 30 minutes.
POLL_TIMEOUT_SECONDS = 30 * 60

# Environment variable for the DynamoDB table name — same as other tools.
JOBS_TABLE_NAME_ENV_VAR = "JOBS_TABLE_NAME"

# Lazily-initialized DynamoDB Table resource (warm-start reuse pattern,
# consistent with job_status_update.py).
_jobs_table = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ[JOBS_TABLE_NAME_ENV_VAR]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def poll_for_confirmation(job_id: str) -> Optional[List[int]]:
    """Poll DynamoDB for ``confirmedSelections`` on the given job record.

    Returns:
        A list of confirmed Detection_Value IDs if the user confirms
        within the timeout window, or ``None`` if the timeout expires
        (meaning the caller should redact ALL detected values).
    """
    logger.info(
        "Starting confirmation poll for job %s (interval=%ds, timeout=%ds)",
        job_id,
        POLL_INTERVAL_SECONDS,
        POLL_TIMEOUT_SECONDS,
    )

    elapsed = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        # Deliberate fixed polling interval while waiting for the user's review.
        time.sleep(POLL_INTERVAL_SECONDS)  # nosemgrep: arbitrary-sleep
        elapsed += POLL_INTERVAL_SECONDS

        item = _get_jobs_table().get_item(Key={"jobId": job_id}).get("Item")
        if item and "confirmedSelections" in item:
            selections = item["confirmedSelections"]
            logger.info(
                "Confirmation received for job %s after %ds: %d IDs selected",
                job_id,
                elapsed,
                len(selections),
            )
            return [int(sid) for sid in selections]

    logger.warning(
        "Confirmation poll timed out for job %s after %ds — will redact all",
        job_id,
        POLL_TIMEOUT_SECONDS,
    )
    return None
