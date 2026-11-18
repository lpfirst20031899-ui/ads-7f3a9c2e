"""Київський зсув від UTC — одне правило на обидва проксі.

Коли воно лежало в кожному проксі окремо, помилка теж жила в двох копіях.

CRM віддає час у UTC (`ordered_at` із `Z`), а дата заявки на екрані має бути
київською, інакше заявка з 23:30 за Києвом падає в наступний день.
"""
from datetime import datetime, timedelta, timezone


def last_sunday_utc(year, month):
    """Остання неділя місяця, 01:00 UTC — момент переходу за правилом ЄС."""
    # Березень і жовтень обидва 31-денні, тому відлік від 31-го безпечний.
    d = datetime(year, month, 31, 1, 0, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def offset_hours(dt_utc):
    """+3 на літній час, +2 на зимовий.

    Місяцями це рахувати не можна: правило `3 if 4 <= month <= 10 else 2` тримало
    +3 до кінця жовтня, хоча 2026 рік перейшов на зимовий час 25.10. Через це
    заявки з 23:00 до 00:00 за Києвом з 25 по 31 жовтня записувались наступним
    днем. Та сама помилка навпаки — в березні: з 29.03 уже +3, а правило давало +2.
    """
    return 3 if last_sunday_utc(dt_utc.year, 3) <= dt_utc < last_sunday_utc(dt_utc.year, 10) else 2


def iso_to_dmy(iso):
    """'2026-10-28T21:30:00Z' → '29.10.2026' влітку, '28.10.2026' взимку."""
    if not iso:
        return ''
    try:
        dt = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        dt_ua = dt.astimezone(timezone(timedelta(hours=offset_hours(dt))))
        return f"{dt_ua.day:02d}.{dt_ua.month:02d}.{dt_ua.year}"
    except Exception:
        return ''


def dmy_to_iso(dmy):
    """'18.11.2026' → '2026-11-18'. Для порівняння дат заявок із межею вікна синку."""
    p = str(dmy or '').split(' ')[0].split('.')
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else ''


def demo():
    from datetime import timezone as tz
    u = lambda s: datetime.fromisoformat(s).replace(tzinfo=tz.utc)
    assert last_sunday_utc(2026, 10).date().isoformat() == '2026-10-25'
    assert last_sunday_utc(2026, 3).date().isoformat() == '2026-03-29'
    assert last_sunday_utc(2027, 10).date().isoformat() == '2027-10-31'
    assert offset_hours(u('2026-10-24T12:00:00')) == 3, 'до переходу ще літній'
    assert offset_hours(u('2026-10-25T00:00:00')) == 3, 'перехід о 01:00 UTC, не о 00:00'
    assert offset_hours(u('2026-10-25T01:00:00')) == 2, 'з 01:00 UTC уже зимовий'
    assert offset_hours(u('2026-03-29T00:00:00')) == 2, 'до 01:00 UTC ще зимовий'
    assert offset_hours(u('2026-03-29T01:00:00')) == 3
    assert offset_hours(u('2026-01-15T12:00:00')) == 2
    assert offset_hours(u('2026-07-15T12:00:00')) == 3
    # Те, на чому ловилась стара формула: 23:30 за Києвом 28.10 (зимовий час).
    assert iso_to_dmy('2026-10-28T21:30:00Z') == '28.10.2026'
    assert iso_to_dmy('2026-10-28T22:30:00Z') == '29.10.2026'
    # Той самий час, але до переходу — зсув +3, дата інша.
    assert iso_to_dmy('2026-10-20T21:30:00Z') == '21.10.2026'
    assert iso_to_dmy('') == ''
    assert iso_to_dmy('не дата') == ''
    assert dmy_to_iso('18.11.2026') == '2026-11-18'
    assert dmy_to_iso('18.11.2026 20:34') == '2026-11-18'
    assert dmy_to_iso('') == ''
    # Порівняння з межею вікна — рядками, тому нуль у дні й місяці обов'язковий.
    assert dmy_to_iso('01.10.2026') >= '2026-09-30'
    assert dmy_to_iso('30.09.2026') < '2026-10-01'
    print('ok')


if __name__ == '__main__':
    demo()
