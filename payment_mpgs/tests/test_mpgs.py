from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_mpgs.controllers.main import MPGSController

REQUEST_PATH = (
    'odoo.addons.payment_mpgs.models.payment_provider.PaymentProvider._mpgs_make_request'
)
SECRET = 'a' * 32


@tagged('post_install', '-at_install')
class TestMPGS(PaymentHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enable_reconcile_after_done_patcher = False  # Exercise the real account.payment flow.
        if not cls.env.company.chart_template:
            # Confirmed donations create an account.payment in the provider's bank journal.
            cls.env['account.chart.template'].try_loading(
                'generic_coa', company=cls.env.company, install_demo=False
            )
        cls.provider = cls._prepare_provider('mpgs', update_values={
            'mpgs_merchant_id': 'TESTISTIDAMA',
            'mpgs_api_password': 'dummy-api-password',
            'mpgs_webhook_secret': SECRET,
        })
        cls.provider.journal_id = cls.env['account.journal'].search(
            [('company_id', '=', cls.env.company.id), ('type', '=', 'bank')], limit=1
        )
        cls.currency = cls.currency_usd
        cls.reference = 'tx-20261109120000'

    def _tx(self, **values):
        values.setdefault('reference', self.reference)
        return self._create_transaction('redirect', **values)

    def _open_session(self, tx):
        tx.write({'mpgs_session_id': 'SESSION0002', 'mpgs_success_indicator': 'ok-indicator'})

    def _order(self, status, amount=None, currency='USD', gateway_code='APPROVED'):
        return {
            'result': 'SUCCESS',
            'id': self.reference,
            'status': status,
            'amount': amount if amount is not None else self.amount,
            'currency': currency,
            'transaction': [{
                'result': 'SUCCESS',
                'response': {'gatewayCode': gateway_code},
                'transaction': {'receipt': '123456789012'},
            }],
        }

    NOT_FOUND = {'result': 'ERROR', 'error': {'cause': 'INVALID_REQUEST',
                                              'explanation': 'Unable to find order'}}

    def _return_url(self, tx, route=MPGSController._return_url):
        return tx._mpgs_build_url(route)

    def _post_webhook(self, body, secret=SECRET):
        url = self._build_url(MPGSController._webhook_url)
        headers = {'Content-Type': 'application/json', 'X-Notification-ID': 'N1'}
        if secret:
            headers['X-Notification-Secret'] = secret
        return self.opener.post(url, json=body, headers=headers)

    # --- Checkout ---------------------------------------------------------------------------

    def test_initiate_checkout_payload_and_redirect(self):
        tx = self._tx()
        response = {'result': 'SUCCESS', 'session': {'id': 'SESSION0002'},
                    'successIndicator': 'ok-indicator'}
        with patch(REQUEST_PATH, return_value=response) as request_mock:
            processing_values = tx._get_processing_values()
        endpoint = request_mock.call_args.args[0]
        payload = request_mock.call_args.kwargs['payload']
        self.assertEqual(endpoint, 'session')
        self.assertEqual(payload['apiOperation'], 'INITIATE_CHECKOUT')
        self.assertEqual(payload['interaction']['operation'], 'PURCHASE')
        self.assertEqual(payload['order'], {
            'id': self.reference, 'amount': '1111.11', 'currency': 'USD',
            'description': f'Payment {self.reference}',
        })
        self.assertIn(f'/payment/mpgs/return/{self.reference}/', payload['interaction']['returnUrl'])
        self.assertEqual(tx.mpgs_session_id, 'SESSION0002')
        self.assertEqual(tx.mpgs_success_indicator, 'ok-indicator')
        form = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        self.assertTrue(form['action'].endswith(MPGSController._checkout_url))
        self.assertEqual(form['inputs']['reference'], self.reference)
        # The secret success indicator is never sent to the browser.
        self.assertNotIn('ok-indicator', processing_values['redirect_form_html'])

    def test_initiate_checkout_error_raises(self):
        tx = self._tx()
        with patch(REQUEST_PATH, return_value=self.NOT_FOUND), \
                mute_logger('odoo.addons.payment_mpgs.models.payment_transaction'):
            with self.assertRaises(ValidationError):
                tx._get_processing_values()

    def test_checkout_page_renders_session(self):
        tx = self._tx()
        self._open_session(tx)
        url = self._build_url(MPGSController._checkout_url)
        response = self._make_http_get_request(url, params={
            'reference': tx.reference, 'access_token': tx._mpgs_get_access_token(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('SESSION0002', response.text)
        self.assertIn('checkout.min.js', response.text)
        self.assertNotIn('ok-indicator', response.text)

    def test_checkout_page_rejects_bad_token(self):
        tx = self._tx()
        self._open_session(tx)
        url = self._build_url(MPGSController._checkout_url)
        response = self._make_http_get_request(url, params={
            'reference': tx.reference, 'access_token': 'forged',
        })
        self.assertEqual(response.status_code, 404)

    # --- Return / cancel ----------------------------------------------------------------------

    def test_return_success_verified_server_side(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED')) as request_mock:
            self._make_http_get_request(self._return_url(tx), params={'resultIndicator': 'ok-indicator'})
        self.assertEqual(request_mock.call_args.args[0], f'order/{self.reference}')
        self.assertEqual(request_mock.call_args.kwargs['method'], 'GET')
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.provider_reference, '123456789012')

    def test_forged_result_indicator_does_not_confirm(self):
        """ A matching-looking return is ignored if the gateway says the payment failed. """
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('FAILED', gateway_code='DECLINED')):
            self._make_http_get_request(self._return_url(tx), params={'resultIndicator': 'ok-indicator'})
        self.assertEqual(tx.state, 'error')

    def test_return_decline_sets_error(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('FAILED', gateway_code='DECLINED')):
            self._make_http_get_request(self._return_url(tx))
        self.assertEqual(tx.state, 'error')
        self.assertIn('DECLINED', tx.state_message)

    def test_cancel_without_payment_cancels(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self.NOT_FOUND):
            self._make_http_get_request(self._return_url(tx, MPGSController._cancel_url))
        self.assertEqual(tx.state, 'cancel')

    def test_return_with_bad_token_is_rejected(self):
        tx = self._tx()
        self._open_session(tx)
        url = self._build_url(f'{MPGSController._return_url}/{tx.reference}/forged')
        with patch(REQUEST_PATH) as request_mock:
            response = self._make_http_get_request(url)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(request_mock.called)
        self.assertEqual(tx.state, 'draft')

    def test_amount_mismatch_sets_error(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED', amount=1.0)), \
                mute_logger('odoo.addons.payment_mpgs.models.payment_transaction'):
            self._make_http_get_request(self._return_url(tx))
        self.assertEqual(tx.state, 'error')

    def test_currency_mismatch_sets_error(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED', currency='LBP')), \
                mute_logger('odoo.addons.payment_mpgs.models.payment_transaction'):
            self._make_http_get_request(self._return_url(tx))
        self.assertEqual(tx.state, 'error')

    def test_gateway_unreachable_leaves_tx_open(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, side_effect=ValidationError('down')), \
                mute_logger('odoo.addons.payment_mpgs.controllers.main'):
            response = self._make_http_get_request(self._return_url(tx))
        self.assertEqual(response.status_code, 200)  # Redirected to the status page.
        self.assertEqual(tx.state, 'draft')

    # --- Webhook ------------------------------------------------------------------------------

    @mute_logger('odoo.addons.payment_mpgs.controllers.main')
    def test_webhook_rejects_bad_secret(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH) as request_mock:
            response = self._post_webhook({'order': {'id': tx.reference}}, secret='wrong')
            missing = self._post_webhook({'order': {'id': tx.reference}}, secret=None)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(missing.status_code, 403)
        self.assertFalse(request_mock.called)
        self.assertEqual(tx.state, 'draft')

    def test_webhook_ignores_body_status_and_verifies(self):
        """ The body claims CAPTURED but the gateway says FAILED: nothing is confirmed. """
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('FAILED')):
            response = self._post_webhook({'order': {'id': tx.reference, 'status': 'CAPTURED'}})
        self.assertEqual(response.status_code, 200)
        # Not final: the donor may still retry on the gateway page.
        self.assertEqual(tx.state, 'draft')

    def test_duplicate_webhooks_are_idempotent(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED')):
            first = self._post_webhook({'order': {'id': tx.reference}})
            self.assertEqual(tx.state, 'done')
            changed_at = tx.last_state_change
            second = self._post_webhook({'order': {'id': tx.reference}})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.last_state_change, changed_at)

    def test_webhook_unknown_order_acknowledged(self):
        with patch(REQUEST_PATH) as request_mock, \
                mute_logger('odoo.addons.payment_mpgs.controllers.main'):
            response = self._post_webhook({'order': {'id': 'unknown-ref'}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(request_mock.called)

    # --- Reconciliation cron ------------------------------------------------------------------

    def _age(self, tx, minutes):
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            [fields.Datetime.now() - timedelta(minutes=minutes), tx.id],
        )
        tx.invalidate_recordset(['create_date'])

    def test_cron_confirms_paid_after_browser_closed(self):
        tx = self._tx()
        self._open_session(tx)
        self._age(tx, 30)
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED')), \
                patch.object(self.env.cr, 'commit'):
            self.env['payment.transaction']._cron_mpgs_reconcile()
        self.assertEqual(tx.state, 'done')

    def test_cron_leaves_recent_unpaid_open_then_cancels_abandoned(self):
        tx = self._tx()
        self._open_session(tx)
        self._age(tx, 30)
        with patch(REQUEST_PATH, return_value=self.NOT_FOUND), \
                patch.object(self.env.cr, 'commit'):
            self.env['payment.transaction']._cron_mpgs_reconcile()
            self.assertEqual(tx.state, 'draft')
            self._age(tx, 180)
            self.env['payment.transaction']._cron_mpgs_reconcile()
        self.assertEqual(tx.state, 'cancel')

    def test_cron_skips_very_recent(self):
        tx = self._tx()
        self._open_session(tx)
        with patch(REQUEST_PATH) as request_mock, patch.object(self.env.cr, 'commit'):
            self.env['payment.transaction']._cron_mpgs_reconcile()
        self.assertFalse(request_mock.called)

    # --- Donation notifications ---------------------------------------------------------------

    def test_staff_donation_notice_waits_for_payment(self):
        tx = self._tx(is_donation=True)
        template = 'odoo.addons.mail.models.mail_template.MailTemplate.send_mail'
        with patch(template) as send_mail:
            tx._send_donation_email(True, 'Keep going', 'staff@example.com')
            self.assertFalse(send_mail.called)
            self.assertTrue(tx.mpgs_staff_notice_pending)

            tx._set_done()
            tx._finalize_post_processing()
        recipients = [c.kwargs['email_values']['email_to'] for c in send_mail.call_args_list]
        self.assertEqual(sorted(recipients), sorted([tx.partner_email, 'staff@example.com']))
        self.assertFalse(tx.mpgs_staff_notice_pending)
        self.assertTrue(tx.payment_id, "A confirmed donation must create an account.payment")

    def test_api_version_must_support_initiate_checkout(self):
        with self.assertRaises(ValidationError):
            self.provider.mpgs_api_version = '62'

    # --- End to end through Odoo's donation routes ----------------------------------------------

    def test_anonymous_donation_end_to_end(self):
        """ Anonymous visitor: donation form -> checkout session -> gateway page -> return. """
        self.provider.is_published = True
        values = self._prepare_transaction_values(self.payment_method_id, None, 'redirect')
        values.update({
            'amount': 37.5,
            'partner_id': False,
            'tokenization_requested': False,
            'reference_prefix': None,
            'landing_route': '/payment/status',
            'partner_details': {
                'name': 'Donor', 'email': 'donor@example.com', 'country_id': self.country_france.id,
            },
            'donation_comment': 'For the project',
            'donation_recipient_email': 'staff@example.com',
        })
        session = {'result': 'SUCCESS', 'session': {'id': 'SESSION0002'},
                   'successIndicator': 'ok-indicator'}
        template = 'odoo.addons.mail.models.mail_template.MailTemplate.send_mail'
        with patch(REQUEST_PATH, return_value=session) as request_mock, patch(template) as send_mail:
            processing_values = self.make_jsonrpc_request(
                self._build_url('/donation/transaction/5'), values
            )
            payload = request_mock.call_args.kwargs['payload']
            self.assertEqual(payload['order']['amount'], '37.50')
            self.assertTrue(payload['order']['description'].startswith('Donation '))
            self.assertFalse(send_mail.called, "No staff email before the payment is confirmed")

        tx = self.env['payment.transaction'].search([('reference', '=', payload['order']['id'])])
        self.assertTrue(tx.is_donation)
        self.assertEqual((tx.amount, tx.partner_email), (37.5, 'donor@example.com'))

        form = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        page = self._make_http_get_request(form['action'], params=form['inputs'])
        self.assertEqual(page.status_code, 200)
        self.assertIn('SESSION0002', page.text)

        self.reference = tx.reference
        with patch(REQUEST_PATH, return_value=self._order('CAPTURED', amount='37.50')), \
                patch(template) as send_mail:
            self._make_http_get_request(self._return_url(tx), params={'resultIndicator': 'ok-indicator'})
            self.assertEqual(tx.state, 'done')
            # The status page polls this route, which finalizes the donation.
            self.make_jsonrpc_request(self._build_url('/payment/status/poll'), {})
        recipients = sorted(c.kwargs['email_values']['email_to'] for c in send_mail.call_args_list)
        self.assertEqual(recipients, ['donor@example.com', 'staff@example.com'])
        self.assertTrue(tx.is_post_processed)
        self.assertTrue(tx.payment_id)
