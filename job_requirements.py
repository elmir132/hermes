"""Bounded, literal requirement hints. Posting text is data, never instructions."""
import html
import re


def notes(title, text, years=None):
    # Feeds often keep HTML codes such as &#39; and &lt; in the text; decode them (twice, for
    # double-escaped feeds) and drop tags, so a snippet doesn't stop at the code's ';'.
    text = html.unescape(html.unescape((title or '') + '. ' + (text or '')))
    text = re.sub(r'</?(?:p|br|li|div|ul|ol|tr|h[1-6])\b[^>]*>', '. ', text, flags=re.I)   # a block ends a sentence
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text)
    out = []
    if years:
        out.append(f'Experience: asks for {years}+ years; check first-job eligibility')
    elif not re.search(r'\b(?:new grad|entry[ -]level|junior|university|early career|campus)\b', title or '', re.I):
        out.append('Entry-level fit: unverified')
    for label, pattern in (
        ('Graduation', r'\b(?:class of|graduat(?:ing|ion|es|ed|e))\b[^.!?;]{0,100}'),
        ('Start date', r'\b(?:start(?:ing)?(?: date)?|immediate start)\b[^.!?;]{0,85}'),
    ):
        for match in re.finditer(pattern, text, re.I):
            snippet = re.sub(rf'^{label}\s*:\s*', '', match[0].strip(), flags=re.I)   # 'Start date: Start date: ...'
            if re.search(r'\b20\d{2}\b|\bimmediate\b', snippet, re.I):
                if len(snippet) > 100:                               # end at a whole word
                    snippet = snippet[:100].rsplit(' ', 1)[0].rstrip(',;:') + '…'
                out.append(f'{label}: {snippet}; verify your availability')
                break
    if re.search(r'\b(?:Ph\.?D\.?|doctorate)\s+(?:is\s+)?required\b|\bmust (?:have|hold|possess)\s+(?:a\s+)?(?:Ph\.?D\.?|doctorate)\b', text, re.I):
        out.append('Degree: PhD explicitly required; check eligibility')
    return out


def flags(job):
    """Refresh hints even for stored listings prepared before this feature."""
    from jobs import required_years
    prefixes = ('Experience:', 'Entry-level fit:', 'Graduation:', 'Start date:', 'Degree:')
    existing = [f for f in job.get('flags', []) if not f.startswith(prefixes)]
    return existing + notes(job.get('title', ''), job.get('text', ''), required_years(job.get('text', '')))
