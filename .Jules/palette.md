## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Adaptive Card Notification Summaries for Microsoft Teams

In plain English: When Teams delivers an Adaptive Card notification, popup toasts and screen readers show generic fallback text like "Sent an attachment" unless a plain-text summary is explicitly provided. Adding a root-level `"summary"` property ensures notifications and screen readers announce the alert text directly.

**Learning:** Microsoft Teams popup toasts and screen reader announcements rely on a top-level `"summary"` property in Adaptive Card payloads. Without it, notifications display generic attachment fallbacks or blank previews.
**Action:** Include a plain-text `"summary"` property at the message and Adaptive Card root levels when sending notification cards to Microsoft Teams webhooks.
