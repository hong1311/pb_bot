import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

from pb_bot.core.sessions import to_ny


@dataclass(frozen=True)
class NewsEvent:
    event_date: date
    event_time_ny: time
    title: str
    impact: str  # "RED", "ORANGE", "YELLOW"

    @property
    def is_red(self) -> bool:
        return self.impact.upper() == "RED"

    @property
    def is_nfp_or_cpi(self) -> bool:
        t = self.title.upper()
        return any(k in t for k in ("NFP", "NON-FARM", "NONFARM", "CPI", "CONSUMER PRICE"))


class EconomicCalendar:
    """Forex Factory news regime filter (Red Folder blocks; Yellow Folder ignored)."""

    def __init__(self, events: Optional[List[NewsEvent]] = None) -> None:
        # Ignore Yellow folder events completely per Recognizing Market Conditions
        self.events: List[NewsEvent] = [
            e for e in (events or []) if e.impact.upper() in ("RED", "ORANGE")
        ]

    @classmethod
    def from_csv(cls, csv_path: Path) -> "EconomicCalendar":
        """Loads calendar CSV with columns: date (YYYY-MM-DD), time_ny (HH:MM), title, impact."""
        loaded: List[NewsEvent] = []
        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["date"].strip())
                hh, mm = row["time_ny"].strip().split(":")
                loaded.append(
                    NewsEvent(
                        event_date=d,
                        event_time_ny=time(int(hh), int(mm)),
                        title=row["title"].strip(),
                        impact=row["impact"].strip().upper(),
                    )
                )
        return cls(loaded)

    def has_pm_red_news(self, trading_day: date) -> Optional[NewsEvent]:
        """Returns PM Red Folder event (>= 12:00 NY, e.g. FOMC) that suppresses AM session."""
        for e in self.events:
            if e.event_date == trading_day and e.is_red and e.event_time_ny >= time(12, 0):
                return e
        return None

    def is_day_before_nfp_or_cpi(self, trading_day: date) -> Optional[NewsEvent]:
        """Returns tomorrow's NFP/CPI Red Folder event if today is the pre-event chop day."""
        next_day = trading_day + timedelta(days=1)
        for e in self.events:
            if e.event_date == next_day and e.is_red and e.is_nfp_or_cpi:
                return e
        return None

    def active_red_news_freeze(
        self, ts_ny: datetime, pre_minutes: int = 2, post_minutes: int = 3
    ) -> Optional[NewsEvent]:
        """Freezes entries immediately around a Red Folder release (e.g., 10:00 AM news)."""
        ts = to_ny(ts_ny)
        for e in self.events:
            if e.event_date != ts.date() or not e.is_red:
                continue
            ev_dt = datetime.combine(e.event_date, e.event_time_ny, tzinfo=ts.tzinfo)
            if (ev_dt - timedelta(minutes=pre_minutes)) <= ts <= (ev_dt + timedelta(minutes=post_minutes)):
                return e
        return None
