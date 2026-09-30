from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

CN = ZoneInfo("Asia/Shanghai")
US = ZoneInfo("America/New_York")
CN_COLLECTION_TIMES = (time(15, 10), time(17, 10), time(19, 10))


@lru_cache(maxsize=8)
def calendar(market, year):
    return xcals.get_calendar("XSHG" if market == "CN" else "XNYS",
                              start=f"{year - 3}-01-01", end=f"{year}-12-31")


def session_close(market, day, config):
    override = config.get("calendar_overrides", {}).get(market, {}).get(day.isoformat())
    if override is False:
        return None
    zone = CN if market == "CN" else US
    if isinstance(override, str):
        return datetime.combine(day, time.fromisoformat(override), zone)
    cal = calendar(market, day.year)
    stamp = pd.Timestamp(day)
    if not cal.is_session(stamp):
        return None
    return cal.session_close(stamp).to_pydatetime()


def ready_at(market, day, config):
    close = session_close(market, day, config)
    if close is None:
        return None
    if market == "CN":
        return datetime.combine(day, CN_COLLECTION_TIMES[0], CN)
    return close + timedelta(hours=1)


def latest_ready(market, config, now=None):
    now = now or datetime.now(timezone.utc)
    local_day = now.astimezone(CN if market == "CN" else US).date()
    for n in range(31):
        day = local_day - timedelta(days=n)
        ready = ready_at(market, day, config)
        if ready and ready <= now:
            return day
    raise ValueError("no completed session found in calendar")


def validate_collection(market, day, config, now=None):
    now = now or datetime.now(timezone.utc)
    # Manual historical collection also respects the first daily collection time.
    if market == "CN" and now.astimezone(CN).time() < CN_COLLECTION_TIMES[0]:
        raise ValueError("CN collection is allowed only from 15:10 Asia/Shanghai")
    ready = ready_at(market, day, config)
    if ready is None or now < ready:
        raise ValueError("target session is closed or not yet ready for EOD collection")


def due_slots(config, now=None):
    now = now or datetime.now(timezone.utc)
    for market in ("CN", "US"):
        if market == "CN":
            day = now.astimezone(CN).date()
            first = ready_at(market, day, config)
            if first is None or now < first:
                continue
            times = [datetime.combine(day, scheduled, CN) for scheduled in CN_COLLECTION_TIMES]
        else:
            day = latest_ready(market, config, now)
            first = ready_at(market, day, config)
            times = [first, first + timedelta(hours=2)]
        # After downtime execute the latest due slot, never all missed slots in a burst.
        eligible = [i for i, when in enumerate(times) if when <= now]
        if eligible:
            index = max(eligible)
            # Clock-based CN keys cannot collide with the previous schedule's numeric slots.
            slot = times[index].strftime("%H%M") if market == "CN" else str(index)
            yield market, day, f"{market}:{day}:{slot}"
