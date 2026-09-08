"""Pay, read out of whatever the posting happens to say.

There is no structured salary field to read. The ATS APIs that return a
description return prose, and the pay is somewhere inside it in one of a dozen
shapes: "$95,000 - $120,000 CAD", "80k-100k", "$45.50 per hour", "Salary
Range: 90000 to 115000 annually". So this is a parser over text, and like
every parser over text it is wrong sometimes.

Three rules keep it honest:

* Silence is not zero. A posting that says nothing about pay returns None, and
  the caller must not read that as "pays badly". Most postings say nothing.
* A number without a currency is not assumed to be anything. A US board saying
  "$95,000" means USD; a Job Bank posting saying "$95,000" means CAD; and
  guessing from the employer's country is how a $95k USD role gets filed at
  $95k CAD and a $60k USD role gets filed as clearing an $80k CAD bar it does
  not clear. Where the currency is not stated the region decides, and the
  result is marked so the dashboard can say so.
* An hourly rate is converted at 2080 hours, which is 40 a week for 52 weeks.
  That is the standard full-time year and it is what the target was expressed
  against.
"""

import re

HOURLY = "hourly"
ANNUAL = "annual"

# 40 hours a week, 52 weeks. The conversion the $80,000 target was stated
# against: $80,000 / 2080 = $38.46 an hour.
FULL_TIME_HOURS = 2080

# Below this an "annual" number is really an hourly rate that lost its unit,
# and above it a "hourly" number is really an annual one. Postings do both.
_MIN_PLAUSIBLE_ANNUAL = 15000
_MAX_PLAUSIBLE_HOURLY = 400

# A number that looks like money: 95,000 | 95000 | 95k | 45.50
_AMOUNT = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{2,7}(?:\.\d{1,2})?|\d{2,3}(?:\.\d)?\s*[kK])\b"
# Two of them with a range separator between.
_RANGE = re.compile(
    _AMOUNT + r"\s*(?:-|–|—|to|through|up to)\s*" + _AMOUNT, re.I)
_SINGLE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d{2,7}(?:\.\d{1,2})?|"
                     r"\d{2,3}(?:\.\d)?\s*[kK])\b")

# Read from a window around the number, not from the whole description: a
# posting mentioning "we are a 100 person company" and, separately, "per hour"
# would otherwise pair them.
_WINDOW = 60
_HOURLY_NEAR = re.compile(
    r"\b(per hour|hourly|an hour|/\s*hr|/\s*hour|hr\b|hour\b)", re.I)
_ANNUAL_NEAR = re.compile(
    r"\b(per year|per annum|annual|annually|a year|/\s*yr|/\s*year|yearly)\b", re.I)
_CAD_NEAR = re.compile(r"\b(cad|c\$|canadian dollars?)\b", re.I)
_USD_NEAR = re.compile(r"\b(usd|us\$|u\.s\. dollars?|american dollars?)\b", re.I)

# Money is only money when the text says so nearby. Without this, "5 years of
# experience" and "over 200,000 customers" both parse as pay.
_PAY_WORD = re.compile(
    r"\b(salar(?:y|ies)|compensation|compensated|pay(?:s|ing)?|paid|"
    r"wage(?:s)?|remuneration|earn(?:s|ing|ings)?|hiring range|"
    r"annualized|per hour|hourly|per year|per annum|annually|ote|"
    r"base (?:pay|salary)|starting (?:at|salary))\b", re.I)


def _to_number(raw):
    text = raw.strip().replace(",", "").replace(" ", "")
    if text[-1:] in ("k", "K"):
        return float(text[:-1]) * 1000
    return float(text)


def _period(window):
    """Hourly or annual, read from the words around the number."""
    if _HOURLY_NEAR.search(window):
        return HOURLY
    if _ANNUAL_NEAR.search(window):
        return ANNUAL
    return None


def _currency(window, region=None):
    """CAD, USD, or None. The region is only consulted as a fallback and the
    caller is told which happened, because the difference decides whether a
    number clears a CAD target."""
    if _CAD_NEAR.search(window):
        return "CAD", True
    if _USD_NEAR.search(window):
        return "USD", True
    if region == "CA":
        return "CAD", False
    if region == "US":
        return "USD", False
    return None, False


def _annualize(low, high, period):
    """Return annual figures, inferring the period when it was not stated."""
    if period is None:
        # No unit given. The magnitude says which it was: nobody is paid
        # $95,000 an hour and nobody is paid $45 a year.
        period = HOURLY if low and low <= _MAX_PLAUSIBLE_HOURLY else ANNUAL
    if period == HOURLY:
        if low and low > _MAX_PLAUSIBLE_HOURLY:
            return None, None, None  # "$95,000 per hour" is a typo, not data
        return (low * FULL_TIME_HOURS if low else None,
                high * FULL_TIME_HOURS if high else None, HOURLY)
    if low and low < _MIN_PLAUSIBLE_ANNUAL:
        return None, None, None
    return low, high, ANNUAL


def parse(text, region=None):
    """The pay a posting states, or None when it states none.

    Returns a dict with the annual figures, the currency, whether that
    currency was stated or inferred from the region, and the period it was
    quoted in. Ranges give both ends; a single figure gives the same number
    twice, because a posting saying "$95,000" is stating one point, not a
    floor of zero.
    """
    if not text:
        return None
    best = None
    for pattern, grouped in ((_RANGE, 2), (_SINGLE, 1)):
        for match in pattern.finditer(text):
            start = max(0, match.start() - _WINDOW)
            window = text[start:match.end() + _WINDOW]
            if not _PAY_WORD.search(window):
                continue
            try:
                low = _to_number(match.group(1))
                high = _to_number(match.group(2)) if grouped == 2 else low
            except (ValueError, IndexError):
                continue
            if high < low:
                low, high = high, low
            low, high, period = _annualize(low, high, _period(window))
            if not low:
                continue
            currency, stated = _currency(window, region)
            found = {
                "min": int(round(low)),
                "max": int(round(high or low)),
                "currency": currency,
                "currency_stated": stated,
                "period": period,
            }
            # A stated range beats a lone figure, and among equals the first
            # one wins: pay is quoted at the top of a posting and the numbers
            # further down are equity, bonuses and headcounts.
            if best is None:
                best = found
        if best is not None:
            break
    return best


def in_cad(pay, rate):
    """The bottom of a pay range in Canadian dollars, or None.

    `rate` is USD to CAD and lives in config rather than here, because it goes
    stale and a number buried in code goes stale silently. It is only ever
    used to compare against a target, never displayed as a converted salary.
    """
    if not pay or not pay.get("min"):
        return None
    currency = pay.get("currency")
    if currency == "CAD":
        return pay["min"]
    if currency == "USD":
        return int(round(pay["min"] * float(rate)))
    return None


def meets(pay, target_cad, rate):
    """Whether the bottom of the range clears the target.

    None means the posting did not say, which is not the same as failing and
    must not be treated as failing: most postings do not say, and the ones
    that do are disproportionately the ones legally required to.
    """
    bottom = in_cad(pay, rate)
    if bottom is None:
        return None
    return bottom >= target_cad
