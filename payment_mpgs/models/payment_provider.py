import logging
import pprint

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment_mpgs import const


_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('mpgs', "Mastercard Gateway (MPGS)")], ondelete={'mpgs': 'set default'}
    )
    mpgs_merchant_id = fields.Char(
        string="MPGS Merchant ID",
        help="The gateway merchant ID, e.g. TESTISTIDAMA for the test account.",
        required_if_provider='mpgs',
    )
    mpgs_api_password = fields.Char(
        string="MPGS API Password",
        help="The Integration Authentication password generated in Merchant Administration "
             "(Admin > Integration Settings). This is NOT the Merchant Administration login "
             "password.",
        required_if_provider='mpgs',
        groups='base.group_system',
    )
    mpgs_gateway_host = fields.Char(
        string="MPGS Gateway Host",
        help="Host name of the gateway, without https://. Test and live hosts are provided by "
             "the bank.",
        default=const.DEFAULT_GATEWAY_HOST,
        required_if_provider='mpgs',
    )
    mpgs_api_version = fields.Char(
        string="MPGS API Version",
        help="REST API version certified by the bank. Hosted Checkout's INITIATE_CHECKOUT "
             "operation requires version 63 or later.",
        default=const.DEFAULT_API_VERSION,
        required_if_provider='mpgs',
    )
    mpgs_webhook_secret = fields.Char(
        string="MPGS Webhook Notification Secret",
        help="The notification secret shown in Merchant Administration "
             "(Admin > Webhook Notifications). Leave empty if webhooks are not enabled; the "
             "reconciliation job still verifies payments.",
        groups='base.group_system',
    )
    mpgs_merchant_name = fields.Char(
        string="Name on Payment Page",
        help="Merchant name displayed on the hosted payment page. Defaults to the company name.",
    )

    # === CONSTRAINT METHODS === #

    @api.constrains('mpgs_api_version')
    def _check_mpgs_api_version(self):
        for provider in self.filtered(lambda p: p.code == 'mpgs' and p.mpgs_api_version):
            version = provider.mpgs_api_version.strip()
            if not version.isdigit() or int(version) < const.MIN_API_VERSION:
                raise ValidationError(_(
                    "The MPGS API version must be a number of at least %s (required by Hosted "
                    "Checkout's INITIATE_CHECKOUT operation).", const.MIN_API_VERSION
                ))

    # === BUSINESS METHODS === #

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        default_codes = super()._get_default_payment_method_codes()
        if self.code != 'mpgs':
            return default_codes
        return const.DEFAULT_PAYMENT_METHODS_CODES

    def _mpgs_get_host(self):
        self.ensure_one()
        host = (self.mpgs_gateway_host or '').strip()
        for prefix in ('https://', 'http://'):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host.split('/')[0]

    def _mpgs_get_checkout_script_url(self):
        self.ensure_one()
        return f'https://{self._mpgs_get_host()}/static/checkout/checkout.min.js'

    def _mpgs_get_api_url(self, endpoint):
        self.ensure_one()
        return 'https://{host}/api/rest/version/{version}/merchant/{merchant}/{endpoint}'.format(
            host=self._mpgs_get_host(),
            version=self.mpgs_api_version.strip(),
            merchant=self.mpgs_merchant_id.strip(),
            endpoint=endpoint,
        )

    def _mpgs_make_request(self, endpoint, payload=None, method='POST'):
        """ Make a server-to-server request to the MPGS REST API.

        Authentication is HTTP Basic with user `merchant.<merchant id>` and the API password.

        :param str endpoint: The endpoint after `/merchant/<id>/`, e.g. `session`.
        :param dict payload: The JSON payload for POST/PUT requests.
        :param str method: The HTTP method.
        :return: The JSON response. Gateway errors (`result == 'ERROR'`) are returned as-is so
                 that callers can decide how to handle them.
        :rtype: dict
        :raise ValidationError: If the gateway cannot be reached or returns no JSON.
        """
        self.ensure_one()
        url = self._mpgs_get_api_url(endpoint)
        auth = (f'merchant.{self.mpgs_merchant_id.strip()}', self.sudo().mpgs_api_password or '')
        try:
            response = requests.request(
                method, url, json=payload, auth=auth, timeout=const.REQUEST_TIMEOUT
            )
        except requests.exceptions.RequestException:
            _logger.exception("MPGS: unable to reach endpoint at %s", url)
            raise ValidationError(
                "MPGS: " + _("Could not establish the connection to the payment gateway.")
            )
        try:
            data = response.json()
        except ValueError:
            _logger.error(
                "MPGS: non-JSON response (HTTP %s) from %s:\n%s",
                response.status_code, url, response.text[:1000],
            )
            raise ValidationError(
                "MPGS: " + _("The payment gateway returned an invalid response (HTTP %s).",
                             response.status_code)
            )
        if response.status_code == 401:
            _logger.error("MPGS: authentication rejected for merchant %s", self.mpgs_merchant_id)
        if data.get('result') == 'ERROR':
            _logger.warning(
                "MPGS: error response (HTTP %s) from %s:\n%s",
                response.status_code, url, pprint.pformat(data.get('error')),
            )
        return data

    @staticmethod
    def _mpgs_get_error_message(data):
        error = data.get('error') or {}
        return error.get('explanation') or error.get('cause') or _("Unknown error")
