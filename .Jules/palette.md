## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Notification summary and fallbackText for Teams Adaptive Cards

**Learning:** When posting Adaptive Cards via Teams incoming webhooks, Teams relies on the root message `summary` and card `fallbackText`/`summary` to generate OS toast notifications, mobile banners, and accessible screen-reader previews. Without these fields, toasts either display generic or blank previews. Furthermore, alert messages containing full URLs can expose sensitive queue or approval links on lock screens or banner notifications. Redacting `http(s)://` URLs to `[link]` and collapsing whitespace preserves security and provides clean, accessible toast announcements within length limits.
**Action:** Always provide a URL-redacted, bounded `summary` on the root message and `summary`/`fallbackText` on the Adaptive Card object for webhook posts.
