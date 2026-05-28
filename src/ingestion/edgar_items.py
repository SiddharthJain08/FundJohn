"""SEC 8-K Item number extraction + canned descriptions.

Pure module, no I/O. Reads `bytes` (or `str`) HTML and extracts
the list of declared 8-K Items via regex.
"""
from __future__ import annotations

import re
from typing import Union


ITEM_DESCRIPTIONS: dict[str, str] = {
    # Section 1 — Registrant's Business and Operations
    '1.01': 'Entry into a Material Definitive Agreement',
    '1.02': 'Termination of a Material Definitive Agreement',
    '1.03': 'Bankruptcy or Receivership',
    '1.04': 'Mine Safety — Reporting of Shutdowns and Patterns of Violations',
    '1.05': 'Material Cybersecurity Incidents',
    # Section 2 — Financial Information
    '2.01': 'Completion of Acquisition or Disposition of Assets',
    '2.02': 'Results of Operations and Financial Condition',
    '2.03': 'Creation of a Direct Financial Obligation',
    '2.04': 'Triggering Events That Accelerate or Increase a Direct Financial Obligation',
    '2.05': 'Costs Associated with Exit or Disposal Activities',
    '2.06': 'Material Impairments',
    # Section 3 — Securities and Trading Markets
    '3.01': 'Notice of Delisting or Failure to Satisfy a Continued Listing Rule',
    '3.02': 'Unregistered Sales of Equity Securities',
    '3.03': 'Material Modification to Rights of Security Holders',
    # Section 4 — Matters Related to Accountants and Financial Statements
    '4.01': "Changes in Registrant's Certifying Accountant",
    '4.02': 'Non-Reliance on Previously Issued Financial Statements',
    # Section 5 — Corporate Governance and Management
    '5.01': 'Changes in Control of Registrant',
    '5.02': ('Departure of Directors or Certain Officers; Election of Directors;'
             ' Appointment of Officers'),
    '5.03': 'Amendments to Articles of Incorporation or Bylaws',
    '5.04': "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    '5.05': "Amendments to the Registrant's Code of Ethics",
    '5.06': 'Change in Shell Company Status',
    '5.07': 'Submission of Matters to a Vote of Security Holders',
    '5.08': 'Shareholder Director Nominations',
    # Section 6 — ABS Issuers and Servicers
    '6.01': 'ABS Informational and Computational Material',
    # Section 7 — Regulation FD
    '7.01': 'Regulation FD Disclosure',
    # Section 8 — Other Events
    '8.01': 'Other Events',
    # Section 9 — Financial Statements and Exhibits
    '9.01': 'Financial Statements and Exhibits',
}

UNPARSED_PLACEHOLDER = 'UNPARSED'
UNPARSED_DESCRIPTION = 'Item extraction failed'

_TAG_RE = re.compile(r'<[^>]+>')
_ITEM_RE = re.compile(r'(?:^|\s)ITEM\s+(\d+\.\d+)', re.IGNORECASE)


def parse_items_from_document(html: Union[str, bytes]) -> list[str]:
    """Extract 8-K Item numbers from a primary document.

    Filters against ITEM_DESCRIPTIONS so unknown numbers (e.g., from
    accidental matches in narrative text) are dropped.

    Returns deduped, ordered list. Empty list when no recognized
    Items are found OR input fails to decode (defensive).
    """
    if not html:
        return []

    if isinstance(html, bytes):
        try:
            text = html.decode('utf-8', errors='replace')
        except (UnicodeDecodeError, AttributeError):
            return []
    else:
        text = html

    cleaned = _TAG_RE.sub(' ', text)
    raw = _ITEM_RE.findall(cleaned)

    seen: set[str] = set()
    out: list[str] = []
    for n in raw:
        if n in seen:
            continue
        if n not in ITEM_DESCRIPTIONS:
            continue
        seen.add(n)
        out.append(n)
    return out
