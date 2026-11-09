import hmac
import json
import logging

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request


_logger = logging.getLogger(__name__)


class MPGSController(http.Controller):
    _checkout_url = '/payment/mpgs/checkout'
    _return_url = '/payment/mpgs/return'
    _cancel_url = '/payment/mpgs/cancel'
    _webhook_url = '/payment/mpgs/webhook'

    @staticmethod
    def _get_tx(reference, access_token):
        """ Return the MPGS transaction for a signed URL, or raise NotFound. """
        tx_sudo = request.env['payment.transaction'].sudo().search(
            [('reference', '=', reference), ('provider_code', '=', 'mpgs')], limit=1
        )
        if not tx_sudo or not tx_sudo._mpgs_check_access_token(access_token):
            raise NotFound()
        return tx_sudo

    @http.route(_checkout_url, type='http', auth='public', methods=['GET'], website=False,
                sitemap=False)
    def mpgs_checkout(self, reference=None, access_token=None, **_kwargs):
        """ Render the page that loads Mastercard's checkout.js and opens the hosted payment page.

        Card details are entered on the gateway only; they never reach Odoo.
        """
        tx_sudo = self._get_tx(reference, access_token)
        if tx_sudo.state != 'draft' or not tx_sudo.mpgs_session_id:
            return request.redirect('/payment/status')
        provider = tx_sudo.provider_id
        return request.render('payment_mpgs.checkout_page', {
            'script_url': provider._mpgs_get_checkout_script_url(),
            'session_id': tx_sudo.mpgs_session_id,
            'cancel_url': tx_sudo._mpgs_build_url(self._cancel_url),
        })

    @http.route(f'{_return_url}/<string:reference>/<string:access_token>', type='http',
                auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def mpgs_return(self, reference, access_token, resultIndicator=None, **_kwargs):
        """ Handle the donor's return from the gateway.

        `resultIndicator` is only logged: the outcome is always read from the gateway with
        Retrieve Order (server-to-server) before the transaction is updated.
        """
        tx_sudo = self._get_tx(reference, access_token)
        _logger.info(
            "MPGS: donor returned for %s (result indicator matches success: %s)",
            reference, tx_sudo._mpgs_result_indicator_matches(resultIndicator),
        )
        self._sync(tx_sudo, final=True)
        return request.redirect('/payment/status')

    @http.route(f'{_cancel_url}/<string:reference>/<string:access_token>', type='http',
                auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def mpgs_cancel(self, reference, access_token, **_kwargs):
        """ Handle the donor leaving the gateway page (cancel button or checkout error). """
        tx_sudo = self._get_tx(reference, access_token)
        _logger.info("MPGS: donor cancelled checkout for %s", reference)
        self._sync(tx_sudo, final=True)
        return request.redirect('/payment/status')

    @http.route(_webhook_url, type='http', auth='public', methods=['POST'], csrf=False,
                save_session=False)
    def mpgs_webhook(self, **_kwargs):
        """ Handle a gateway webhook notification.

        The X-Notification-Secret header is checked, then the notification body is used only to
        find the order: its status is re-read from the gateway. Redelivered notifications are
        harmless because state changes are idempotent. A 200 is returned once the secret is
        valid so that the gateway stops retrying.
        """
        secret = request.httprequest.headers.get('X-Notification-Secret', '')
        providers = request.env['payment.provider'].sudo().search([('code', '=', 'mpgs')])
        if not secret or not any(
            p.mpgs_webhook_secret and hmac.compare_digest(secret, p.mpgs_webhook_secret)
            for p in providers
        ):
            _logger.warning("MPGS: webhook received with missing or invalid secret")
            raise Forbidden()

        reference = self._get_webhook_order_id()
        _logger.info(
            "MPGS: webhook for order %s (notification id %s, attempt %s)",
            reference,
            request.httprequest.headers.get('X-Notification-ID'),
            request.httprequest.headers.get('X-Notification-Attempt'),
        )
        if not reference:
            return ''
        tx_sudo = request.env['payment.transaction'].sudo().search(
            [('reference', '=', reference), ('provider_code', '=', 'mpgs')], limit=1
        )
        if not tx_sudo:
            _logger.warning("MPGS: webhook for unknown order %s; acknowledged", reference)
            return ''
        self._sync(tx_sudo, final=False)
        return ''

    @staticmethod
    def _get_webhook_order_id():
        raw = request.httprequest.get_data(as_text=True) or ''
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if isinstance(body, dict):
            return (body.get('order') or {}).get('id')
        # NVP format fallback.
        return request.httprequest.form.get('order.id')

    @staticmethod
    def _sync(tx_sudo, final):
        try:
            tx_sudo._mpgs_sync_with_gateway(final=final)
        except ValidationError:
            # Gateway unreachable or bad data: leave the transaction open for the cron.
            request.env.cr.rollback()
            _logger.exception("MPGS: could not verify transaction %s", tx_sudo.reference)
