"""The written policy corpus the agent retrieves from.

Synthetic, written for this project, modelled on the customer-facing policies
a UK gift-hamper retailer actually publishes. This is the knowledge the agent
is grounded in: if a claim is not supported here or by live order data, the
verifier rejects it.
"""

from __future__ import annotations

from resolve.domain.schemas import PolicyDocument

POLICY_DOCUMENTS: list[PolicyDocument] = [
    PolicyDocument(
        id="POL-DELIVERY",
        title="Delivery and Dispatch Policy",
        category="delivery",
        body="""
# Standard delivery timeframes

Orders placed before 2pm on a working day are dispatched the same day.
Orders placed after 2pm, at weekends, or on bank holidays are dispatched the
next working day. Standard UK delivery arrives within 2 to 4 working days of
dispatch. Delivery to the Republic of Ireland takes 3 to 6 working days.
Named-day and next-day options are dispatched with a tracked service.

# Tracking

Every dispatched order receives a tracking number by email. Tracking
information can take up to 12 hours after dispatch to appear on the carrier's
website. If tracking has not updated for 3 working days, the parcel is treated
as delayed and we open an enquiry with the carrier.

# Delayed or missing parcels

A UK parcel is not considered lost until 10 working days after dispatch, and an
Irish parcel until 14 working days. Before that point the carrier will not
accept a claim. Where a parcel is confirmed lost we send a replacement at no
cost, or issue a full refund, at the customer's choice.

# Delivery to a business address

Parcels delivered to a business address may be signed for by reception or a
post room. Where the carrier holds a signature, the delivery is treated as
completed and a claim cannot be opened.
""".strip(),
    ),
    PolicyDocument(
        id="POL-REFUNDS",
        title="Refunds and Returns Policy",
        category="refunds",
        body="""
# Right to cancel and return

Under the Consumer Contracts Regulations, customers may cancel an order within
14 days of receiving it and return it for a full refund. The 14-day period
begins the day after delivery.

# Perishable and personalised goods

Food hampers and any product containing perishable items are exempt from the
14-day right to return once dispatched, because they cannot be resold.
Personalised items, including engraved bottles and printed gift cards, are
likewise exempt unless faulty.

# Refund processing time

Approved refunds are issued to the original payment method. Funds take 3 to 5
working days to appear on a debit or credit card statement, and up to 10
working days for some banks. We cannot refund to a different card or account
than the one used for the original payment.

# Damaged or faulty goods

Where goods arrive damaged we issue a replacement or a full refund, including
original delivery charges. Photographs of the damage help but are not required.
Claims for damage should be raised within 30 days of delivery.

# Partial refunds

Where only part of an order is affected, we refund the value of the affected
items plus a proportionate share of the delivery charge.
""".strip(),
    ),
    PolicyDocument(
        id="POL-PAYMENTS",
        title="Payments and Failed Transactions",
        category="payments",
        body="""
# Failed payments

Where a card payment is declined the order is held for 7 days in a pending
state and stock is reserved. We send a secure payment link so the customer can
complete the payment without re-entering the whole order. Payment links expire
24 hours after they are issued; a new link can be generated at any time.

# Duplicate charges

Where a customer sees two charges for one order, the second is usually a
pre-authorisation that has not yet cleared. Pre-authorisations drop off
automatically within 5 working days and no action is needed. Where two payments
have genuinely settled against the same order, the duplicate is refunded in
full and the customer is told the same working day.

# Payment security

We never ask customers for full card numbers, CVV codes or online banking
credentials by email or telephone. Any message requesting these is not from us
and should be reported.

# Chargebacks

Where a chargeback has been raised through the customer's bank we cannot issue
a separate refund for the same transaction, because doing so would refund the
amount twice. The bank's process must complete first.
""".strip(),
    ),
    PolicyDocument(
        id="POL-AMENDMENTS",
        title="Order Amendments and Cancellations",
        category="amendments",
        body="""
# Changing a delivery address

The delivery address can be changed at any time before the order is dispatched.
Once the parcel is with the carrier the address is fixed and we cannot redirect
it. The customer may be able to redirect the parcel directly with the carrier
using their tracking reference.

# Cancelling before dispatch

An order can be cancelled free of charge at any point before dispatch, and the
payment is released the same working day.

# Cancelling after dispatch

Once dispatched, an order cannot be cancelled. The customer may refuse delivery
or return the goods under the returns policy, subject to the exemptions for
perishable and personalised items.

# Changing items or gift messages

Items and gift messages can be amended before dispatch. After dispatch neither
can be changed, and a gift message that has already been printed cannot be
recalled.
""".strip(),
    ),
    PolicyDocument(
        id="POL-PRODUCT",
        title="Product Information, Allergens and Storage",
        category="product",
        body="""
# Allergen information

Every hamper lists its full ingredient and allergen information on the product
page. Hampers containing nuts, gluten, dairy, soya or sulphites are labelled.
We cannot guarantee that any hamper is free from traces of nuts, because items
are packed in a facility that handles them.

# Alcohol

Hampers containing alcohol are only sold to customers aged 18 or over, and the
carrier may ask for identification on delivery. Alcohol cannot be delivered to
some postcodes and cannot be left with a neighbour.

# Storage and shelf life

Hampers should be stored in a cool, dry place. Every perishable item carries a
best-before date of at least 30 days from dispatch. Chilled items are packed
with insulation and should be refrigerated on arrival.

# Substitutions

Where an item is out of stock we substitute an item of equal or greater value
from the same category and note the substitution on the despatch note.
""".strip(),
    ),
    PolicyDocument(
        id="POL-SERVICE",
        title="Customer Service Standards",
        category="service",
        body="""
# Response times

We reply to every customer message within one working day. Messages received at
weekends are answered on the next working day.

# Escalation

Complaints, claims involving a legal threat, and any request we cannot resolve
under published policy are escalated to a senior member of the customer care
team, who responds within two working days.

# Compensation and goodwill

Goodwill gestures are offered at the discretion of the customer care team where
service has fallen below standard. A goodwill gesture is not an admission of
fault and does not affect the customer's statutory rights.

# Data protection

We only discuss an order with the person who placed it, or with someone they
have authorised in writing. We do not confirm order details to a third party.
""".strip(),
    ),
]
