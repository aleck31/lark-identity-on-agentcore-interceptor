"""lark-agent CDK stacks package."""

from aws_cdk import aws_logs as logs

_RETENTION_MAP = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def retention_days(days: int) -> logs.RetentionDays:
    """Convert an integer number of days to the nearest valid RetentionDays enum."""
    if days in _RETENTION_MAP:
        return _RETENTION_MAP[days]
    for d in sorted(_RETENTION_MAP):
        if d >= days:
            return _RETENTION_MAP[d]
    return logs.RetentionDays.ONE_YEAR
