"""Humanized upload scheduling.

Generates `videos_per_day` timestamps inside a daily window with random jitter
so uploads don't look bot-like. Persists slots in the DB and on each Actions
run we publish whatever is "due" right now.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Iterator


def humanized_slots(
    *,
    day: datetime,
    videos_per_day: int,
    window_start_hour: int,
    window_end_hour: int,
    jitter_minutes: int,
) -> list[datetime]:
    """Spread N slots evenly across the window, then add ± random jitter."""
    if videos_per_day <= 0:
        return []
    start = day.replace(hour=window_start_hour, minute=0, second=0, microsecond=0)
    end = day.replace(hour=window_end_hour, minute=0, second=0, microsecond=0)
    span = (end - start).total_seconds()
    base_step = span / videos_per_day
    out: list[datetime] = []
    for i in range(videos_per_day):
        center = start + timedelta(seconds=base_step * (i + 0.5))
        jitter = random.uniform(-jitter_minutes, jitter_minutes)
        out.append(center + timedelta(minutes=jitter))
    out.sort()
    return out


def iter_next_n_days(start: datetime, n: int) -> Iterator[datetime]:
    for i in range(n):
        yield (start + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
