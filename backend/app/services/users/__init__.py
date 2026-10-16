"""User domain: accounts, entitlements, alerts and notifications.

Owns account management, subscription entitlements, API keys, alert/notification
delivery (channel providers), email delivery, price alerts and daily reports.

Dependency rule: may import `platform`, `intelligence`, `market_data`, and
`billing` for subscription events. Must not import `api` or `trading`.
"""
