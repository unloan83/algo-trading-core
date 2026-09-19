#!/usr/bin/env python3
from datetime import date

from scripts.runtime_common import project_config


def main():
    holidays = project_config("timing.yaml")["timing"][
        "nse_holiday_exclusions_2026"
    ]
    parsed = [(date.fromisoformat(value), name) for value, name in holidays.items()]
    if any(day.year != 2026 for day, _ in parsed):
        raise RuntimeError("NSE_HOLIDAY_EXCLUSION_OUTSIDE_2026")
    if any(day.weekday() >= 5 for day, _ in parsed):
        raise RuntimeError("NSE_HOLIDAY_EXCLUSION_FALLS_ON_WEEKEND")
    if parsed != sorted(parsed):
        raise RuntimeError("NSE_HOLIDAY_EXCLUSIONS_NOT_SORTED")

    print(f"nse_2026_holiday_exclusions={len(parsed)}")
    for day, name in parsed:
        print(f"{day.isoformat()} | {name}")


if __name__ == "__main__":
    main()
