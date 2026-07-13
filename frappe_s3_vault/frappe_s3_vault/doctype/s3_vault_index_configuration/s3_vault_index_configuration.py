from __future__ import annotations

from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime


class S3VaultIndexConfiguration(Document):
    def validate(self):
        if not self.status:
            self.status = "Never Synced"
        if not self.enabled or self.sync_frequency == "Manual":
            self.next_sync_on = None
        elif self.has_value_changed("sync_frequency") or not self.next_sync_on:
            self.next_sync_on = next_sync(self.sync_frequency)


def next_sync(frequency: str, from_time=None):
    value = from_time or now_datetime()
    if frequency == "Every 6 Hours":
        return add_to_date(value, hours=6, as_datetime=True)
    if frequency == "Daily":
        return add_to_date(value, days=1, as_datetime=True)
    if frequency == "Weekly":
        return add_to_date(value, days=7, as_datetime=True)
    return None
