"""Constants for the MPGS Hosted Checkout integration.

Order statuses come from the `order.status` field of the Retrieve Order response:
https://developer.mastercard.com/mastercard-gateway/documentation/api-reference/
"""

# Default gateway host: Bank of Beirut's MPGS *test* environment. The live host must be
# confirmed by the bank and entered on the provider before going live.
DEFAULT_GATEWAY_HOST = 'test-bobsal.gateway.mastercard.com'

# Version used in Bank of Beirut's "BOB Gateway Integration" guide. INITIATE_CHECKOUT requires
# API version 63 or later.
DEFAULT_API_VERSION = '73'
MIN_API_VERSION = 63

REQUEST_TIMEOUT = 20  # seconds

# Payment methods activated with the provider.
DEFAULT_PAYMENT_METHODS_CODES = ['card', 'visa', 'mastercard']

# Mapping of MPGS order statuses to Odoo transaction outcomes.
# - done: funds were captured (later refunds/disputes do not undo the donation record in Odoo).
# - pending: authorized but not captured; should not happen with PURCHASE, flagged for review.
# - failed: the payment attempt failed.
# - in_progress: the payer has not finished; only final once the checkout is over.
ORDER_STATUS_MAPPING = {
    'done': (
        'CAPTURED', 'PARTIALLY_REFUNDED', 'REFUNDED', 'REFUND_REQUESTED',
        'EXCESSIVELY_REFUNDED', 'DISPUTED', 'CHARGEBACK_PROCESSED',
    ),
    'pending': ('AUTHORIZED', 'PARTIALLY_CAPTURED'),
    'failed': ('FAILED', 'AUTHENTICATION_UNSUCCESSFUL', 'DECLINED'),
    'cancel': ('CANCELLED',),
    'in_progress': (
        'INITIATED', 'AUTHENTICATION_INITIATED', 'AUTHENTICATED',
        'AUTHENTICATION_NOT_NEEDED', 'VERIFIED',
    ),
}

# Reconciliation cron: transactions younger than this are left alone (the donor may still be
# on the payment page); older than the final delay, an unpaid checkout is closed.
CRON_MIN_AGE_MINUTES = 15
CRON_FINAL_AGE_MINUTES = 120
CRON_LOOKBACK_DAYS = 3
