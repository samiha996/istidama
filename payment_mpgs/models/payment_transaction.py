import hmac
import logging
from datetime import timedelta

from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_repr
from odoo.tools import hmac as hmac_tool

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment_mpgs import const


_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    mpgs_session_id = fields.Char(string="MPGS Checkout Session", readonly=True, copy=False)
    mpgs_success_indicator = fields.Char(
        string="MPGS Success Indicator", readonly=True, copy=False, groups='base.group_system'
    )
    # Staff "a donation has been made" notice, deferred until the payment is confirmed.
    mpgs_staff_notice_pending = fields.Boolean(readonly=True, copy=False)
    mpgs_staff_notice_comment = fields.Text(readonly=True, copy=False)
    mpgs_staff_notice_recipient = fields.Char(readonly=True, copy=False)

    # === REFERENCE === #

    @api.model
    def _compute_reference(self, provider_code, prefix=None, separator='-', **kwargs):
        """ Override of `payment` to produce references valid as MPGS order ids.

        MPGS order ids are limited to 40 characters; document-based prefixes (e.g. INV/2024/...)
        may contain characters the gateway rejects, so a timestamp prefix is forced.
        """
        if provider_code == 'mpgs':
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(provider_code, prefix=prefix, separator=separator, **kwargs)

    # === URLS === #

    def _mpgs_get_access_token(self):
        """ Sign the reference with the database secret (works with or without an HTTP request,
        unlike `payment.utils.generate_access_token`). """
        self.ensure_one()
        return hmac_tool(self.env(su=True), 'payment_mpgs', self.reference)

    def _mpgs_check_access_token(self, access_token):
        self.ensure_one()
        return bool(access_token) and hmac.compare_digest(
            str(access_token), self._mpgs_get_access_token()
        )

    def _mpgs_build_url(self, route):
        """ Build an absolute, signed URL for the given route prefix. The reference and the
        token are path segments so that the gateway can append its own query string. """
        self.ensure_one()
        base_url = self.provider_id.get_base_url()
        path = f'{route}/{urls.url_quote(self.reference, safe="")}/{self._mpgs_get_access_token()}'
        return urls.url_join(base_url, path)

    # === CHECKOUT === #

    def _get_specific_rendering_values(self, processing_values):
        """ Override of `payment` to open an MPGS checkout session and return the values of the
        redirect form, which sends the donor to the page loading the hosted payment page.

        Note: self.ensure_one() from `_get_processing_values`
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'mpgs':
            return res

        self._mpgs_initiate_checkout()
        from odoo.addons.payment_mpgs.controllers.main import MPGSController
        return {
            'api_url': urls.url_join(
                self.provider_id.get_base_url(), MPGSController._checkout_url
            ),
            'reference': self.reference,
            'access_token': self._mpgs_get_access_token(),
        }

    def _mpgs_prepare_checkout_payload(self):
        self.ensure_one()
        from odoo.addons.payment_mpgs.controllers.main import MPGSController
        provider = self.provider_id
        merchant_name = (provider.mpgs_merchant_name or self.company_id.name or '')[:40]
        description = (
            _("Donation %s", self.reference) if getattr(self, 'is_donation', False)
            else _("Payment %s", self.reference)
        )
        return {
            'apiOperation': 'INITIATE_CHECKOUT',
            'interaction': {
                'operation': 'PURCHASE',
                'returnUrl': self._mpgs_build_url(MPGSController._return_url),
                'cancelUrl': self._mpgs_build_url(MPGSController._cancel_url),
                'merchant': {'name': merchant_name},
            },
            'order': {
                'id': self.reference,
                'amount': float_repr(self.amount, self.currency_id.decimal_places),
                'currency': self.currency_id.name,
                'description': description[:127],
            },
        }

    def _mpgs_initiate_checkout(self):
        """ Create the Hosted Checkout session and store its id and success indicator. """
        self.ensure_one()
        data = self.provider_id._mpgs_make_request(
            'session', payload=self._mpgs_prepare_checkout_payload()
        )
        session_id = (data.get('session') or {}).get('id')
        success_indicator = data.get('successIndicator')
        if data.get('result') != 'SUCCESS' or not session_id or not success_indicator:
            # The RPC that created the transaction is rolled back with this error.
            _logger.error(
                "MPGS: could not open checkout session for %s: %s",
                self.reference, self.provider_id._mpgs_get_error_message(data),
            )
            raise ValidationError(
                "MPGS: " + _("The payment could not be started. Please try again later.")
            )
        self.write({
            'mpgs_session_id': session_id,
            'mpgs_success_indicator': success_indicator,
            'provider_reference': self.reference,
        })
        _logger.info(
            "MPGS: opened checkout session %s for transaction %s", session_id, self.reference
        )

    def _mpgs_result_indicator_matches(self, result_indicator):
        self.ensure_one()
        expected = self.sudo().mpgs_success_indicator
        return bool(result_indicator and expected) and hmac.compare_digest(
            str(result_indicator), expected
        )

    # === VERIFICATION === #

    def _mpgs_sync_with_gateway(self, final):
        """ Fetch the order from the gateway (Retrieve Order) and update the transaction.

        This is the only way the transaction state changes: browser returns, cancellations,
        webhooks and the cron all funnel through here, so nothing the browser sends is trusted.

        :param bool final: Whether the checkout is over (donor returned/cancelled, or the cron's
                           final delay elapsed). If not final, unpaid outcomes leave the
                           transaction open since the donor may still retry on the gateway page.
        """
        self.ensure_one()
        # Serialize concurrent syncs (browser return + webhook) on the same transaction.
        self.env.cr.execute(
            'SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE', [self.id]
        )
        self.invalidate_recordset()
        order_data = self.provider_id._mpgs_make_request(
            f'order/{urls.url_quote(self.reference, safe="")}', method='GET'
        )
        self._handle_notification_data('mpgs', {
            'reference': self.reference,
            'order_data': order_data,
            'final': final,
        })

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        """ Override of `payment` to find the transaction from the MPGS order id. """
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'mpgs' or len(tx) == 1:
            return tx

        reference = notification_data.get('reference')
        if not reference:
            raise ValidationError("MPGS: " + _("Received data with missing reference."))
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'mpgs')])
        if not tx:
            raise ValidationError(
                "MPGS: " + _("No transaction found matching reference %s.", reference)
            )
        return tx

    def _process_notification_data(self, notification_data):
        """ Override of `payment` to update the transaction from a Retrieve Order response.

        Note: self.ensure_one()
        """
        super()._process_notification_data(notification_data)
        if self.provider_code != 'mpgs':
            return

        order = notification_data.get('order_data') or {}
        final = notification_data.get('final', False)
        status = order.get('status')

        if order.get('result') == 'ERROR' or not status:
            # Typically "order not found": the donor never submitted a card on the gateway.
            if final:
                self._set_canceled("MPGS: " + _("No payment was made on the gateway."))
            else:
                _logger.info(
                    "MPGS: no gateway order yet for %s (%s)", self.reference,
                    self.provider_id._mpgs_get_error_message(order),
                )
            return

        if order.get('id') and order['id'] != self.reference:
            raise ValidationError("MPGS: " + _("The gateway order does not match the transaction."))

        if status in const.ORDER_STATUS_MAPPING['done'] + const.ORDER_STATUS_MAPPING['pending']:
            mismatch = self._mpgs_get_amount_mismatch(order)
            if mismatch:
                _logger.warning("MPGS: %s for transaction %s", mismatch, self.reference)
                self._set_error("MPGS: " + mismatch)
                return
            receipt = self._mpgs_get_last_transaction_value(order, 'receipt')
            if receipt:
                self.provider_reference = receipt
            if status in const.ORDER_STATUS_MAPPING['done']:
                self._set_done()
            else:
                self._set_pending(
                    "MPGS: " + _("Authorized but not captured (status %s). Check Merchant "
                                 "Administration.", status)
                )
        elif status in const.ORDER_STATUS_MAPPING['failed']:
            if final:
                gateway_code = self._mpgs_get_last_transaction_value(order, 'gatewayCode')
                self._set_error("MPGS: " + _(
                    "The payment was not approved (%(status)s, %(code)s).",
                    status=status, code=gateway_code or _("no code"),
                ))
        elif status in const.ORDER_STATUS_MAPPING['cancel']:
            if final:
                self._set_canceled("MPGS: " + _("The payment was cancelled on the gateway."))
        elif status in const.ORDER_STATUS_MAPPING['in_progress']:
            if final:
                self._set_canceled(
                    "MPGS: " + _("The payment was not completed (status %s).", status)
                )
        else:
            _logger.warning(
                "MPGS: unhandled order status %s for transaction %s", status, self.reference
            )
            if final:
                self._set_error("MPGS: " + _("Unexpected order status %s.", status))

    def _mpgs_get_amount_mismatch(self, order):
        """ Return an error message if the gateway amount/currency differ from the transaction. """
        currency = order.get('currency')
        if currency != self.currency_id.name:
            return _("Currency mismatch: expected %(expected)s, gateway has %(got)s.",
                     expected=self.currency_id.name, got=currency)
        try:
            amount = float(order.get('amount'))
        except (TypeError, ValueError):
            return _("The gateway order has no amount.")
        if self.currency_id.compare_amounts(amount, self.amount) != 0:
            return _("Amount mismatch: expected %(expected)s, gateway has %(got)s.",
                     expected=self.amount, got=amount)
        return ''

    @staticmethod
    def _mpgs_get_last_transaction_value(order, key):
        """ Read `transaction.receipt` or `response.gatewayCode` from the latest order transaction. """
        transactions = order.get('transaction') or []
        if not isinstance(transactions, list) or not transactions:
            return None
        last = transactions[-1]
        if key == 'gatewayCode':
            return (last.get('response') or {}).get('gatewayCode')
        return (last.get('transaction') or {}).get(key)

    # === RECONCILIATION CRON === #

    @api.model
    def _cron_mpgs_reconcile(self):
        """ Verify open MPGS transactions with the gateway.

        Covers donors who paid and then closed the browser before returning to the website,
        missed webhooks, and abandoned checkouts (closed after the final delay).
        """
        now = fields.Datetime.now()
        txs = self.search([
            ('provider_code', '=', 'mpgs'),
            ('state', '=', 'draft'),
            ('create_date', '<=', now - timedelta(minutes=const.CRON_MIN_AGE_MINUTES)),
            ('create_date', '>=', now - timedelta(days=const.CRON_LOOKBACK_DAYS)),
        ])
        final_before = now - timedelta(minutes=const.CRON_FINAL_AGE_MINUTES)
        for tx in txs:
            final = tx.create_date <= final_before
            try:
                if not tx.mpgs_session_id:
                    if final:  # The checkout was never opened.
                        tx._set_canceled("MPGS: " + _("The checkout session was never opened."))
                else:
                    tx._mpgs_sync_with_gateway(final=final)
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("MPGS: reconciliation failed for transaction %s", tx.reference)

    # === DONATION NOTIFICATIONS === #

    def _send_donation_email(self, is_internal_notification=False, comment=None, recipient_email=None):
        """ Override of `website_payment` to defer the staff notice until the payment is done.

        Odoo sends "A donation has been made on your website" when the transaction is created,
        i.e. also for abandoned and declined attempts.
        """
        if (
            is_internal_notification
            and self.provider_code == 'mpgs'
            and self.state != 'done'
            and not self.env.context.get('mpgs_send_staff_notice')
        ):
            self.write({
                'mpgs_staff_notice_pending': True,
                'mpgs_staff_notice_comment': comment,
                'mpgs_staff_notice_recipient': recipient_email,
            })
            return None
        return super()._send_donation_email(
            is_internal_notification=is_internal_notification,
            comment=comment,
            recipient_email=recipient_email,
        )

    def _finalize_post_processing(self):
        """ Override of `website_payment` to send the deferred staff notice. """
        super()._finalize_post_processing()
        for tx in self.filtered(lambda t: t.provider_code == 'mpgs' and t.mpgs_staff_notice_pending):
            tx.with_context(mpgs_send_staff_notice=True)._send_donation_email(
                True, tx.mpgs_staff_notice_comment, tx.mpgs_staff_notice_recipient
            )
            tx.mpgs_staff_notice_pending = False
