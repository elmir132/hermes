"""Validate an exported original alert locally, without network or database writes.

Exported headers are not proof of sender authentication. Review Gmail's original
message and exact sender before enabling the existing allowlisted mail worker.
"""
import argparse
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
import json
from pathlib import Path

import digest
import job_alerts

MAX_MESSAGE_BYTES = 2_000_000


def validate(raw):
    if len(raw) > MAX_MESSAGE_BYTES:
        return {'sender': '', 'complete': False, 'listings': [], 'warnings': ['Message exceeds validation size limit']}
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get('From', '')))[1].lower()
    sender_valid = bool(digest.parse_job_alert_senders([sender]))
    parts, warnings = [], []
    for part in message.walk():
        if part.get_content_disposition() == 'attachment' or part.get_filename():
            continue
        if part.get_content_type() == 'text/html':
            try:
                parts.append(part.get_content())
            except (LookupError, UnicodeError, ValueError):
                warnings.append('HTML part could not be decoded')
    body = '\n'.join(parts)
    records, complete = job_alerts.parse_email(body[:job_alerts.MAX_BODY])
    if len(body) >= job_alerts.MAX_BODY:
        warnings.append('HTML exceeds parser size limit')
        complete = False
    if not sender_valid:
        warnings.append('Missing or invalid exact sender address')
    if not parts:
        warnings.append('No supported HTML listing template; plain text requires parser support')
    if not complete:
        warnings.append('Not all listings could be validated; do not enable this template yet')
    warnings.append('Exported headers do not verify sender authentication; check Gmail Show original')
    return {'sender': sender, 'complete': bool(complete and sender_valid),
            'listings': [{key: row[key] for key in ('title', 'company', 'location', 'url')} for row in records],
            'warnings': warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('message', type=Path, help='Exported original .eml file; keep private')
    args = parser.parse_args()
    with args.message.open('rb') as stream:
        result = validate(stream.read(MAX_MESSAGE_BYTES + 1))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
