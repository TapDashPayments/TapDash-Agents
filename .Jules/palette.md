## 2026-10-22 - Full-width Adaptive Cards in Microsoft Teams
**Learning:** Default Adaptive Cards in Microsoft Teams render in a narrow ~400px column, causing multi-line system alerts and long tool names to wrap awkwardly. Adding `"msteams": {"width": "Full"}` at the card root allows alert cards to expand to the full container width for improved readability.
**Action:** Always include `"msteams": {"width": "Full"}` when generating Adaptive Cards for Microsoft Teams notifications.
