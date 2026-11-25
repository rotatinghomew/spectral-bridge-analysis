from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from check_sources import load_sources


def _parse_day(text: str) -> date | None:
    return date.fromisoformat(text) if text else None


def _parse_slot(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _parse_float(text: str) -> float:
    return float(text)


def _flag(text: str) -> bool:
    return text == '1'


@dataclass(frozen=True)
class Instrument:
    id: str
    site: str
    valid_from: date
    valid_until: date | None
    series: str
    min: float
    max: float

    def covers_day(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_until is None or day < self.valid_until

    def in_window(self, value: float) -> bool:
        return self.min <= value <= self.max


@dataclass(frozen=True)
class Slice:
    id: str
    sheet: str
    swap: str
    series: str
    purpose: str
    lot: str


@dataclass(frozen=True)
class Reading:
    id: str
    slice: str
    instrument: str
    day: date
    received: date
    value: float


@dataclass(frozen=True)
class PairChange:
    id: str
    received: date
    slice: str
    withdraw_swap: str


@dataclass(frozen=True)
class Order:
    id: str
    lot: str
    min: float
    max: float
    allowed_error: float
    line_only: bool
    lab_allowed: bool
    shipped_at: date | None


@dataclass(frozen=True)
class Certificate:
    id: str
    orders: tuple[str, ...]
    group_max_mean_error: float
    common_reader_bias: bool
    partial_allowed: bool
    issued: date | None


@dataclass(frozen=True)
class Retest:
    id: str
    slice: str
    instrument: str
    owner: str
    slot: datetime
    state: str


class Repository:
    """只读资料仓：解析类型并提供按可见时点查询的便利方法。"""

    def __init__(self, directory: str | Path):
        rows = load_sources(Path(directory))
        self.instruments: dict[str, Instrument] = {
            r['id']: Instrument(
                r['id'], r['site'], date.fromisoformat(r['from']),
                _parse_day(r['until']), r['series'],
                _parse_float(r['min']), _parse_float(r['max']),
            )
            for r in rows['instruments']
        }
        self.slices: dict[str, Slice] = {
            r['id']: Slice(r['id'], r['sheet'], r['swap'], r['series'],
                          r['purpose'], r['lot'])
            for r in rows['slices']
        }
        self.readings = [
            Reading(r['id'], r['slice'], r['instrument'],
                    date.fromisoformat(r['day']),
                    date.fromisoformat(r['received']),
                    _parse_float(r['value']))
            for r in rows['readings']
        ]
        self.pair_changes = [
            PairChange(r['id'], date.fromisoformat(r['received']),
                       r['slice'], r['withdraw_swap'])
            for r in rows['pair_changes']
        ]
        self.orders: dict[str, Order] = {
            r['id']: Order(
                r['id'], r['lot'], _parse_float(r['min']),
                _parse_float(r['max']), _parse_float(r['allowed_error']),
                _flag(r['line_only']), _flag(r['lab_allowed']),
                _parse_day(r['shipped_at']),
            )
            for r in rows['orders']
        }
        self.certificates: dict[str, Certificate] = {
            r['id']: Certificate(
                r['id'], tuple(part for part in r['orders'].split(',') if part),
                _parse_float(r['group_max_mean_error']),
                _flag(r['common_reader_bias']),
                _flag(r['partial_allowed']), _parse_day(r['issued']),
            )
            for r in rows['certificates']
        }
        self.retests = [
            Retest(r['id'], r['slice'], r['instrument'], r['owner'],
                   _parse_slot(r['slot']), r['state'])
            for r in rows['retests']
        ]
        self._slices_by_sheet: dict[str, list[str]] = {}
        for sl in self.slices.values():
            self._slices_by_sheet.setdefault(sl.sheet, []).append(sl.id)
        self._slices_by_lot: dict[str, list[str]] = {}
        for sl in self.slices.values():
            if sl.lot:
                self._slices_by_lot.setdefault(sl.lot, []).append(sl.id)

    def slices_of_sheet(self, sheet: str) -> list[str]:
        return list(self._slices_by_sheet.get(sheet, ()))

    def order_slices(self, lot: str) -> list[Slice]:
        return [self.slices[sid] for sid in self._slices_by_lot.get(lot, ())
                if self.slices[sid].purpose == 'order']

    def withdrawn_swaps(self, as_of: date) -> set[str]:
        return {pc.withdraw_swap for pc in self.pair_changes
                if pc.received <= as_of}

    def visible_readings(self, as_of: date) -> list[Reading]:
        return [r for r in self.readings if r.received <= as_of]

    def instrument_measurement(self, reading: Reading) -> bool:
        inst = self.instruments[reading.instrument]
        return (inst.covers_day(reading.day)
                and inst.series == self.slices[reading.slice].series
                and inst.in_window(reading.value))
