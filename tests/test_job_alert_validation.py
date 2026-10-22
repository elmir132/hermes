from email.message import EmailMessage
from pathlib import Path
import unittest
from unittest.mock import patch

import job_alert_validation as validation

SAMPLE = (Path(__file__).parent/'fixtures/job_alert_cards.html').read_text()


def mail(body=SAMPLE, sender='Verified Alerts <alerts@example.org>', html=True):
    message = EmailMessage()
    message['From'] = sender
    message.set_content(body, subtype='html' if html else 'plain')
    return message


class ValidationTests(unittest.TestCase):
    def test_original_mime_lists_each_company_without_fetching_links(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            result = validation.validate(mail().as_bytes())
        self.assertTrue(result['complete'])
        self.assertEqual(result['sender'], 'alerts@example.org')
        self.assertEqual(len(result['listings']), 2)
        self.assertEqual({r['company'] for r in result['listings']}, {'Example Alpha', 'Example Beta'})
        self.assertTrue(all(r['title'] and r['location'] and r['url'] for r in result['listings']))
        self.assertIn('do not verify sender', ' '.join(result['warnings']))

    def test_plain_text_newsletter_and_missing_sender_fail_closed(self):
        for message in (mail('Newsletter without listings'), mail('Software Engineer', html=False), mail(sender='')):
            with self.subTest(message=message.get_content_type()):
                self.assertFalse(validation.validate(message.as_bytes())['complete'])

    def test_attachments_are_ignored(self):
        message = mail('Newsletter without listings')
        message.add_attachment(SAMPLE.encode(), maintype='text', subtype='html', filename='untrusted.html')
        self.assertEqual(validation.validate(message.as_bytes())['listings'], [])

    def test_bounded_message_and_html(self):
        result = validation.validate(b'x'*(validation.MAX_MESSAGE_BYTES+1))
        self.assertFalse(result['complete'])
        result = validation.validate(mail(SAMPLE+'x'*200001).as_bytes())
        self.assertFalse(result['complete'])
