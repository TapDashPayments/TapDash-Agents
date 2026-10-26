## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-26 - Adaptive Card Notifications and Screen Reader Previews in Microsoft Teams

**Learning:** Adaptive Cards sent to Microsoft Teams webhooks without a root-level `summary` property render as generic "Adaptive Card" or blank previews in Teams notification toasts, popups, and screen reader announcements. Adding `"summary": text` at the `AdaptiveCard` root level provides accessibility screen readers and OS/Teams notification banners with explicit plain text content.
**Action:** Always include `"summary": text` at the root level of Adaptive Cards sent to Microsoft Teams webhooks so notifications and screen readers display meaningful alert text.
