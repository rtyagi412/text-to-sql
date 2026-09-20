Organisation-wide conventions for the payment-gateway reporting database. These apply to every report and take precedence over guesses about what a term means.

Storage conventions
- Monetary amounts are stored as integer minor units (e.g. cents), and the column names end in `_units` (`amount_units`, `net_amount_units`, `refund_amount_units`). Show and compare them as stored: a ticket threshold such as "amount greater than 5000" is compared directly against the `_units` column. Do not convert to a major currency unit unless the ticket asks.
- Categorical values (statuses, methods, roles, environments) are stored in UPPER_SNAKE_CASE. A ticket's "Paid" is `'PAID'`, "Live" is `'LIVE'`, "Card" is `'CARD'`.
- Relative reporting windows ("last 30 days", "past 6 months") are measured back from the current UTC time (`SYSUTCDATETIME()`).

Business vocabulary
- "Active merchant" means the merchant's status is `ACTIVE`.
- "Dashboard user" means a row of the application-user table; "owner or admin" refers to its role.
- "Dead-lettered" webhook events are webhook events whose status is `DEAD`; "replayed" means the dead-letter event's replayed-at timestamp is not null, so "not replayed" is `IS NULL`.
