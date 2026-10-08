"""Small helpers with no Slack or DB dependencies."""


def hhmm_to_minutes(s):
    """'09:30' -> 570 (minutes from midnight)."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(m):
    """570 -> '09:30'."""
    return f"{m // 60:02d}:{m % 60:02d}"
