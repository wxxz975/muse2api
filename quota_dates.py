"""Conservative reset-date metadata; month/day text never becomes an exact time."""
from __future__ import annotations
from datetime import date, datetime, timezone
import re
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
MONTHS = {name: i for i, name in enumerate(("jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"),1)}


def reset_metadata(raw, checked_at: int) -> dict:
    reference = datetime.fromtimestamp(checked_at, ZONE).date()
    if isinstance(raw, (int,float)) and not isinstance(raw,bool) and 946684800 <= raw <= 7258118400:
        moment = datetime.fromtimestamp(raw, timezone.utc)
        return {"weekly_reset_at": int(raw), "weekly_reset_date": moment.astimezone(ZONE).date().isoformat(),
                "weekly_reset_timezone": "Asia/Shanghai", "weekly_reset_precision": "timestamp"}
    if not isinstance(raw,str):
        return {}
    value = raw.strip()
    try:
        if "T" in value or re.search(r"\d{2}:\d{2}",value):
            moment = datetime.fromisoformat(value.replace("Z","+00:00"))
            if moment.tzinfo is not None:
                return reset_metadata(moment.timestamp(),checked_at)
            return {}  # No time zone, no exact countdown.
        day = date.fromisoformat(value)
    except ValueError:
        day = None
    if day is None:
        match = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?",value)
        month, number, year = None, None, None
        if match:
            month = MONTHS.get(match.group(1).lower()[:3]); number=int(match.group(2))
            year = int(match.group(3)) if match.group(3) else None
        else:
            match=re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月\s*(\d{1,2})[日号]?",value)
            if match:
                year=int(match.group(1)) if match.group(1) else None
                month,number=int(match.group(2)),int(match.group(3))
        if not month or not number:
            return {}
        candidates=[]
        for candidate_year in ([year] if year else [reference.year-1,reference.year,reference.year+1]):
            try:candidates.append(date(candidate_year,month,number))
            except ValueError:pass
        if not candidates:return {}
        day=min(candidates,key=lambda d:abs((d-reference).days))
        # A weekly date without a year is useful only near the time it was observed.
        if year is None and not -1 <= (day-reference).days <= 8:return {}
    return {"weekly_reset_date":day.isoformat(), "weekly_reset_timezone":"Asia/Shanghai",
            "weekly_reset_precision":"date"}
