## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Notification summary and fallbackText for Teams Adaptive Cards

**Learning:** When sending Adaptive Cards via Microsoft Teams webhooks, notification banners, push notifications, and screen readers rely on root `summary` and card `fallbackText` fields. Without them, notifications may show generic text or fail to speak the card's content to assistive tech. Furthermore, alert text often includes approval URLs that should not leak onto mobile lock screens; redacting links to `[link]` prevents exposure while preserving message context.
**Action:** Always set `summary` on root message payloads and `fallbackText` on Adaptive Cards, redacting sensitive URLs to `[link]` and bounding character length.
