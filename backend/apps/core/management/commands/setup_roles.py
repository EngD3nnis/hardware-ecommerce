"""Create/refresh the standard staff groups and their permissions. Safe to re-run."""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = "view"
ROLES = {
    # Everything, including approvals, refunds, automation control.
    "Owner": "__all__",
    "Shop attendant": [
        "catalog.view_product",
        "catalog.view_category",
        "pricing.view_productprice",
        "inventory.view_stockbalance",
        "customers.add_customer",
        "customers.change_customer",
        "customers.view_customer",
        "sales.add_quotation",
        "sales.change_quotation",
        "sales.view_quotation",
        "sales.view_order",
        "sales.change_order",
        "payments.add_payment",
        "payments.view_payment",
        "fulfillment.view_fulfillment",
        "fulfillment.change_fulfillment",
        "notifications.view_outboundmessage",
        "notifications.view_inboundmessage",
        "notifications.change_inboundmessage",
        "automation.view_internaltask",
        "automation.change_internaltask",
    ],
    "Storekeeper": [
        "catalog.view_product",
        "inventory.view_stockbalance",
        "inventory.change_stockbalance",
        "inventory.view_stockmovement",
        "inventory.add_stockadjustment",
        "inventory.view_stockadjustment",
        "inventory.add_stockcount",
        "inventory.change_stockcount",
        "inventory.view_stockcount",
        "inventory.add_stockcountline",
        "inventory.change_stockcountline",
        "inventory.view_reservation",
        "procurement.view_purchaseorder",
        "procurement.change_purchaseorder",
        "procurement.add_purchaseorder",
        "procurement.view_supplier",
        "fulfillment.view_fulfillment",
        "fulfillment.change_fulfillment",
        "sales.view_order",
        "automation.view_internaltask",
        "automation.change_internaltask",
    ],
    "Accounts": [
        "payments.view_payment",
        "payments.add_payment",
        "payments.view_refund",
        "payments.add_refund",
        "payments.record_refund",
        "payments.view_inboundpaymentevent",
        "sales.view_order",
        "sales.view_quotation",
        "customers.view_customer",
        "procurement.view_purchaseorder",
        "procurement.approve_purchaseorder",
        "procurement.view_supplier",
        "audit.view_auditevent",
    ],
}


class Command(BaseCommand):
    help = "Create the standard staff groups (Owner, Shop attendant, Storekeeper, Accounts)."

    def handle(self, *args, **options):
        for name, codes in ROLES.items():
            group, _ = Group.objects.get_or_create(name=name)
            if codes == "__all__":
                perms = Permission.objects.filter(
                    content_type__app_label__in=[
                        "catalog",
                        "pricing",
                        "inventory",
                        "procurement",
                        "customers",
                        "sales",
                        "payments",
                        "fulfillment",
                        "notifications",
                        "automation",
                        "ai",
                        "analytics",
                        "search",
                        "core",
                        "audit",
                    ]
                )
            else:
                perms = [
                    Permission.objects.get(content_type__app_label=c.split(".")[0], codename=c.split(".")[1])
                    for c in codes
                ]
            group.permissions.set(perms)
            self.stdout.write(f"{name}: {len(perms)} permissions")
        self.stdout.write("Assign people to groups in Admin → Users (and tick 'staff status').")
