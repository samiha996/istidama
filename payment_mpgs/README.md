# payment_mpgs — Mastercard Gateway (MPGS) Hosted Checkout for Odoo 17

Lets visitors donate by card from Odoo 17 Community's built-in **Donation** block
(`website_payment`). There's no shop, cart or sales order. Card details are entered only on
the bank's hosted payment page; they never reach Odoo.

Built for ISTIDAMA / Bank of Beirut (test host `test-bobsal.gateway.mastercard.com`).

## How a donation flows

1. The donor picks a preset or custom amount in the Donation block, fills in name, email and country
   on `/donation/pay`, and clicks **Donate**.
2. Odoo creates a draft transaction and calls MPGS **INITIATE_CHECKOUT** server-to-server
   (operation `PURCHASE`, order id = transaction reference, amount, currency, return and cancel URLs).
   It stores the session id and the secret `successIndicator`.
3. The donor is sent to `/payment/mpgs/checkout`, which loads Mastercard's `checkout.min.js` and
   opens the **Hosted Payment Page**. Card entry and 3-D Secure happen there.
4. The gateway sends the donor back to `/payment/mpgs/return/<reference>/<signed token>`.
5. Odoo calls **Retrieve Order** server-to-server and confirms the order is `CAPTURED` with the same
   amount and currency. Only then is the transaction marked *Confirmed*. The browser's
   `resultIndicator` is logged and never trusted.
6. The donor lands on `/payment/status`. Odoo then sends the donor confirmation and the staff
   notice, and creates an `account.payment` in the provider's journal.

### Safety nets

| Situation | What happens |
|---|---|
| Donor pays, then closes the browser | The webhook (if configured) or the reconciliation cron (every 15 min) runs Retrieve Order and confirms it. |
| Abandoned checkout | Left open for 2 hours, since the donor may still be paying, then cancelled by the cron. |
| Declined card | The donor can retry on the gateway page. When they return or cancel, the transaction is set to *Error* with the gateway code. |
| Duplicate or replayed webhooks | The secret is checked, then the order is re-read from the gateway. Odoo ignores repeated state changes, so there's no second payment or email. |
| Forged return URL / indicator | Signed URL required; the state always comes from Retrieve Order. |
| Gateway unreachable | Transaction stays open; the cron retries. |
| Amount or currency differs at the gateway | Transaction set to *Error*, never *Confirmed*. |

It also fixes an Odoo behavior: core `website_payment` emails "A donation has been made"
to staff when the transaction is **created**, so every abandoned or declined attempt triggers one.
For MPGS transactions the add-on holds that notice back until the payment is confirmed.

## Install

Requirements: Odoo 17 Community with `website` and `website_payment` (which pulls in Invoicing).
`website_sale` is not needed.

On Cloudpepper:

1. Push the `payment_mpgs` folder to a Git repository, at the root of the repo or in a subfolder of addons.
2. On the **staging** instance, go to **Addons** and add the repository (private repo: add Cloudpepper's
   SSH key as a deploy key).
3. In Odoo: **Apps → Update Apps List**, search for "MPGS", then **Activate**.

## Configure

### 1. Merchant Administration (bank portal, in a browser)

The **portal login** (Merchant ID + operator + password + Microsoft Authenticator) is for people.
It never goes into Odoo.

- **API password:** Admin → Integration Settings → Edit (Integration Authentication) →
  check *Password 1* → **Generate New**, tick **Enable Integration Access Via Password** → Submit.
  Copy it. This is what Odoo uses (API user `merchant.<Merchant ID>`).
- **Webhooks (recommended):** Admin → Webhook Notifications.
  - URL: `https://<your-domain>/payment/mpgs/webhook`
  - Format: **REST-JSON**
  - Copy the **Notification Secret**.

### 2. Odoo: Website → Configuration → Payment Providers → *Card (Mastercard Gateway)*

| Field | Test value |
|---|---|
| State | **Test Mode** |
| Merchant ID | `TESTISTIDAMA` (see note below) |
| API Password | the Integration Authentication password |
| Gateway Host | `test-bobsal.gateway.mastercard.com` |
| API Version | `73` (the version in Bank of Beirut's integration guide; must be ≥ 63) |
| Webhook Notification Secret | from Merchant Administration |
| Name on Payment Page | e.g. `ISTIDAMA` |

Then:

- **Configuration tab → Payment Journal:** pick the Bank journal for the Bank of Beirut USD account.
  Odoo needs a journal to record confirmed donations, and its donation code fails without one.
- **Currencies:** restrict to the currency the bank confirms (USD expected). The company's or
  website's currency must match, because the Donation block uses it.
- **Published:** on, and restrict to the website.

Merchant ID note: the bank's email writes `testistidama`. Mastercard's convention is the upper-case
`TEST` prefix. If authentication fails (HTTP 401 in the server log), try the exact case the bank uses.

### 3. Donate page

Website → New Page "Donate" → drag the **Donation** block. In its options:

- set preset amounts and the minimum;
- set Custom Amount to *Input*;
- set the recipient email for staff notices.

Add the page to the menu. Bank of Beirut's presentation also asks merchants to show its logos on
the payment page: https://www.bankofbeirut.com/misc/BOB_Logo200X100.jpg and
https://www.bankofbeirut.com/misc/BOB_eCommerce.gif (add them as images in the Donate page).

## Test (test mode)

On the test gateway the **expiry date and CVV choose the result**, not the card number. Bank of
Beirut's 2018 presentation shows `05/21` approved and `05/22` declined (CVV `100`), but those dates
are now in the past. Use the current values from the bank's test page (link in their email), which
in today's Mastercard gateways are typically `01/39` approved and `05/39` declined, CVV `100`, with
card `5123450000000008`.

- [ ] Custom amount (e.g. 37.50): the same amount shows on the gateway page, the Odoo transaction and Merchant Administration.
- [ ] Approved card: transaction *Confirmed*, one donor email, one staff email, one Payment. Reloading the return URL changes nothing.
- [ ] Declined card (05/39): transaction not confirmed, no confirmation email.
- [ ] Abandoned checkout: close the tab on the gateway page. The transaction stays draft, then is cancelled about 2 hours later by the cron.
- [ ] Pay, then close the tab before returning: confirmed by webhook or cron within 15 minutes.
- [ ] Merchant Administration shows each test order under the Odoo reference.

Logs: search the Odoo server log for `MPGS:`.

## Going live

1. The bank provides the live merchant ID, the **live host** and live portal access.
2. Generate a new API password in the **live** Merchant Administration. Set the webhook URL there too.
3. On production, enter the live values and set State to **Enabled**.
4. Make one small real donation and refund it from Merchant Administration.

## Limitations

- Refunds and voids are done in Merchant Administration, not from Odoo.
- No saved cards (tokenization) and no embedded or lightbox mode; redirect to the Hosted Payment Page only.
- `PURCHASE` (immediate capture) only. If the bank's profile forces authorize-then-capture,
  transactions stop at *Pending* and need capturing in Merchant Administration.
- Odoo's database neutralization (`odoo-bin neutralize`, used for staging copies) clears the
  API password and webhook secret; re-enter test values on staging.
- Timing constants (cron delays) are in `const.py`.

## Tests

```
odoo-bin -d <db> -u payment_mpgs --test-tags /payment_mpgs --stop-after-init
```

22 tests with mocked gateway responses: checkout payload, signed URLs, success/decline/cancel,
forged indicator, amount/currency mismatch, gateway outage, webhook secret, duplicate webhooks,
cron reconciliation and an end-to-end anonymous donation through Odoo's donation routes.
