# 0009: Quotes convert to orders; order and payment status are separate; webhooks are stored first

**Status:** Accepted, 2026-09-26 (Stage 4)

## Decision
- **Quote-first.** A quotation takes list prices from the pricing service. Lines with no list price stay unpriced ("price on request") until a person prices them. Agents can draft quotations but never set prices. `convert_to_order` copies the lines exactly, is idempotent (one order per quotation, enforced by a DB one-to-one), and reserves stock if available. If stock is short, the order stays CONFIRMED with a note; it is never lost.
- **Snapshots.** Quote and order lines store SKU, name, unit, attributes and price, so history never changes when the catalogue does.
- **Two order dimensions.** `status` tracks the goods (a fixed transition table). `payment_status` tracks the money and is derived from `Payment`/`Refund` rows, never set by hand. The brief's combined list (PAID, PAYMENT_PENDING, REFUNDED…) is covered by the combination of the two. This avoids impossible mixed states such as "PICKING but also PAID".
- **Cancelled orders cannot silently keep money.** Cancel is refused while there are unrefunded payments. Refunds need the `record_refund` permission and a reason, and agents can do neither.
- **Webhooks: store, then process.** The M-Pesa callback is authenticated by a secret URL token (plus an optional IP allow-list), stored in `InboundPaymentEvent` (unique per CheckoutRequestID), acknowledged, and processed in Celery. It only completes a payment we initiated, and only for the exact amount requested. A beat task processes events whose task never ran.
- quotations and orders share one `sales` app (a deviation from the target layout's separate apps) to keep related code together.

## Consequences
Duplicate or forged callbacks cannot create payments. A human can always record a cash/bank/paybill payment manually. A Daraja outage only affects STK push.
