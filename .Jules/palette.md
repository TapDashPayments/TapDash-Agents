## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Teams message summary and card fallbackText for toast notifications and screen readers

In plain English: When Adaptive Cards are posted to Microsoft Teams webhooks, the outer message object needs `"summary": text` and the inner AdaptiveCard needs `"fallbackText": text` so notification toasts, popups, and screen readers display/announce the plain-text alert rather than a generic "Sent a card" placeholder.

**Learning:** In Microsoft Teams webhook payloads, `summary` belongs on the root message object (`{"type": "message", "summary": text, ...}`) while `fallbackText` belongs on the Adaptive Card object (`card.fallbackText = text`). Including both ensures notification toasts, banners, and screen readers convey the alert text across all Teams client platforms.
**Action:** Always provide `"summary": text` on the root message and `"fallbackText": text` on the Adaptive Card attachment when posting to Microsoft Teams webhooks.
