"""Conservative annual compensation evidence; no salary estimates or market data.

Unpriced bonuses, benefits, and multi-year stock grants never prove a total.
An explicitly posted total can reject a role; an incomplete subtotal cannot.
"""
import re

NUMBER = r'(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*[kK]|\d{4,7})'
MONEY = re.compile(r'\$\s*('+NUMBER+r')(?:\s*(?:-|–|—|to|and)\s*\$?\s*('+NUMBER+r'))?', re.I)
GAP = re.compile(r'[\s:()\-–—]*(?:(?:range|for this (?:role|position)|for the (?:role|position)|is|of|between|from|ranges|expected|to be|will be)\s*[:()\-–—]*\s*)*', re.I)
NON_ANNUAL = re.compile(r'\b(?:hourly|monthly|weekly|per (?:hour|month|week)|over (?:\d+|four|three|five) years|first[- ]year|year one|first 12 months)\b', re.I)
FOREIGN = re.compile(r'\b(?:CAD|AUD|SGD|HKD|NZD)\b|C\$|A\$', re.I)
ANNUAL = re.compile(r'\b(?:per year|annually|annualized|each year|a year)\b', re.I)
BASE = r'\bbase (?:salary|pay|compensation)\b'
TOTAL = r'\b(?:annual total compensation|total (?:annual )?compensation|total comp)\b'
BONUS = r'\b(?:annual (?:target |cash |performance )?bonus|target annual bonus)\b'
EQUITY = r'\b(?:annualized (?:equity|stock|RSUs?)|annual (?:equity|stock|RSU) compensation)\b'
SIGNON = r'\bsign(?:ing|-on| on)[ -]bonus\b'


def _amount(value):
    value = value.replace(',', '').replace(' ', '')
    return round(float(value[:-1])*1000) if value[-1:].lower()=='k' else round(float(value))


def _ranges(text, label, annual_required=False, allow_first_year=False):
    found = []
    for match in re.finditer(label, text, re.I):
        tail = text[match.end():match.end()+150]
        money = MONEY.search(tail[:100])
        if not money or not GAP.fullmatch(tail[:money.start()]):
            continue
        suffix = re.split(r'[;\n]|(?<=[.!?])\s+', tail[money.end():], maxsplit=1)[0][:65]
        context = text[max(0,match.start()-24):match.start()] + text[match.start():match.end()] + tail[:money.end()] + suffix
        if FOREIGN.search(context):
            continue
        if not allow_first_year and NON_ANNUAL.search(context):
            continue
        if annual_required and not ANNUAL.search(suffix):
            continue
        lo, hi = _amount(money[1]), _amount(money[2] or money[1])
        if not (0 < lo <= hi <= 3_000_000):
            continue
        found.append((lo, hi))
    # Multiple location/tier ranges are not added together or chosen optimistically.
    return list(dict.fromkeys(found))


def _one(ranges):
    return ranges[0] if len(ranges)==1 else None


def _bonus(text, base):
    fixed = _one(_ranges(text, BONUS))
    if fixed or not base:
        return fixed
    values = []
    for label in re.finditer(BONUS, text, re.I):
        tail = text[label.end():label.end()+100]
        match = re.match(r'\s*(?:is|of|:)?\s*(\d+(?:\.\d+)?)\s*%(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*%)?\s+of\s+(?:annual\s+)?base(?:\s+salary|\s+pay)?\b', tail, re.I)
        if match:
            lo, hi = float(match[1]), float(match[2] or match[1])
            if 0 <= lo <= hi <= 1000:
                values.append((round(base[0]*lo/100), round(base[1]*hi/100)))
    return _one(list(dict.fromkeys(values)))


def analyze(text, base=None, base_trusted=False, target=170000):
    text = text or ''
    total_ranges = _ranges(text, TOTAL)
    total = _one(total_ranges)
    explicit_base = _one(_ranges(text, BASE))
    if explicit_base:
        base = explicit_base
    elif not base_trusted:
        base = None
    bonus = _bonus(text, base)
    equity = _one(_ranges(text, EQUITY))
    if equity is None:
        equity = _one(_ranges(text, r'\b(?:equity|RSUs?|stock compensation)\b', annual_required=True))
    signon = _one(_ranges(text, SIGNON, allow_first_year=True))
    components = [v for v in (base,bonus,equity) if v]
    subtotal = tuple(sum(v[i] for v in components) for i in (0,1)) if components else None
    # Conflicting explicit totals need review, even when a component looks high.
    if len(total_ranges)>1:
        status = 'unknown'
    elif total:
        status = 'below' if total[1]<target else ('meets' if total[0]>=target else 'possible')
    elif subtotal and subtotal[0]>=target:
        status = 'meets'
    elif subtotal and subtotal[1]>=target:
        status = 'possible'
    else:
        status = 'unknown'  # A low base/subtotal is not an upper bound on total pay.
    return dict(status=status,total=total,base=base,bonus=bonus,equity=equity,
                subtotal=subtotal,signon=signon,target=target)


def _label(value):
    def money(v):
        return f'${v:,.0f}'
    return money(value[0]) if value[0]==value[1] else money(value[0])+'–'+money(value[1])


def notes(result):
    """One short line per role (the base range is already on the pay line)."""
    status, target = result['status'], f"${result['target']/1000:g}k"
    amount = result['total'] or result['subtotal']
    shown = f" ({_label(amount)}{' stated total' if result['total'] else ''})" if amount and not _same_as_base(result) else ''
    bonus = ', bonus not guaranteed' if result['bonus'] and not result['total'] else ''
    note = {'meets': f"{target}+ total: yes{shown}{bonus}",
            'possible': f"{target} total: possible{shown}{bonus}",
            'below': f"below the {target} total{shown}",
            'unknown': "total pay: unknown"}[status]
    out = [note]
    if result['signon']:
        out.append('sign-on '+_label(result['signon'])+' (one-time, not counted)')
    return out


def _same_as_base(result):
    return not result['total'] and result['subtotal'] == result['base']
