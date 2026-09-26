"""Default policy — pass through all detections unchanged."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.policy.base import BasePolicy


class DefaultPolicy(BasePolicy):
    """Passes all detections through unchanged."""

    name = "default"

    def _decide_sync(self, detection: Detection, context: Context) -> Detection:
        return detection
