## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams

In plain English: Teams alert cards used to squish into a narrow ~400px column, so long tool names and multi-line alerts wrapped awkwardly. Adding `"msteams": {"width": "Full"}` at the card root makes the card fill the whole Teams chat width, so alerts are easier to read.

**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.

## 2026-10-27 - Root-level Summary Property for Teams Adaptive Cards

**Learning:** Adaptive Cards posted to Microsoft Teams without a root-level `summary` property fail to announce card content in notification toasts, mobile lock-screen popups, and assistive screen reader previews. Providing a plain-text `summary` at the Adaptive Card root level ensures notifications and accessibility tools can immediately announce the alert message without depending on full card rendering.
**Action:** Always include `"summary": <plain text>` at the Adaptive Card root level alongside `"msteams": {"width": "Full"}` when constructing Teams notifications.
