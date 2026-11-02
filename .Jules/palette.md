## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Teams message summary, speak, and card fallbackText for toast notifications and screen readers

In plain English: When Adaptive Cards are posted to Microsoft Teams webhooks, the outer message object needs `"summary": text`, and the inner AdaptiveCard needs `"speak": text` (for screen readers) and `"fallbackText": text` (for unsupported clients) with URLs sanitized to `[link]` so notification toasts, popups, and screen readers announce the alert safely without leaking approval URLs or exceeding payload size bounds.

**Learning:** In Microsoft Teams webhook payloads, `summary` belongs on the root message object (`{"type": "message", "summary": text, ...}`), `speak` is the Adaptive Card schema property for screen reader announcements, and `fallbackText` is for clients that cannot render the card. Redacting sensitive URLs to `[link]` and bounding length prevents leaking sensitive links to lock-screen toasts while staying within Teams' 28 KB payload limit.
**Action:** Always sanitize URLs, enforce payload bounds, and provide `"summary"` on the root message alongside `"speak"` and `"fallbackText"` on the Adaptive Card when posting to Microsoft Teams webhooks.
