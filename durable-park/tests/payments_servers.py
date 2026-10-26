"""What the accept.blue and Authorize.net MCP servers say about each of their tools (shared by several tests).

``ACCEPTBLUE_APPROVALS`` and ``AUTHNET_APPROVALS`` are the output of ``python -m <package> --list-approvals`` at the
pinned commit (the commit in ``deploy/tunnel/acceptblue-mcp.sha`` and ``deploy/tunnel/authorizenet-mcp.sha``):
``auto`` = the server says no person is needed, ``you`` = a person approves. Move a pin, and these lists change in
the same commit.
"""

ACCEPTBLUE_COMMIT = "9bb8d722a0d5fc9dce08437eedc8328d34005bcc"
AUTHORIZENET_COMMIT = "4dd427863d1103c788bfa03d3dfde46777bde3a0"

ACCEPTBLUE_APPROVALS = {
    "ablue_adjust_transaction": "you",
    "ablue_cancel_invoice": "you",
    "ablue_capture_authorization": "you",
    "ablue_change_webhook": "you",
    "ablue_charge_saved_payment_method": "you",
    "ablue_create_customer": "auto",
    "ablue_create_customer_from_transaction": "auto",
    "ablue_create_invoice": "auto",
    "ablue_create_recurring_schedule": "you",
    "ablue_credit_saved_payment_method": "you",
    "ablue_delete_customer": "you",
    "ablue_delete_invoice": "you",
    "ablue_delete_payment_method": "you",
    "ablue_delete_recurring_schedule": "you",
    "ablue_describe_lookup": "auto",
    "ablue_queue_authorization": "you",
    "ablue_read": "auto",
    "ablue_refund_transaction": "you",
    "ablue_request_final_payment": "you",
    "ablue_save_payment_method_from_transaction": "you",
    "ablue_search_lookups": "auto",
    "ablue_send_invoice": "you",
    "ablue_unblock_transaction": "you",
    "ablue_update_customer": "auto",
    "ablue_update_invoice": "you",
    "ablue_update_payment_method": "you",
    "ablue_update_recurring_schedule": "you",
    "ablue_void_transaction": "you",
}

AUTHNET_APPROVALS = {
    "authnet_cancel_subscription": "you",
    "authnet_capture_authorization": "you",
    "authnet_charge_customer_profile": "you",
    "authnet_create_payment_form": "you",
    "authnet_create_profile_form": "you",
    "authnet_create_subscription": "you",
    "authnet_decide_held_transaction": "you",
    "authnet_delete_customer_record": "you",
    "authnet_delete_webhook": "you",
    "authnet_describe_lookup": "auto",
    "authnet_ping_webhook": "you",
    "authnet_read": "auto",
    "authnet_refund_transaction": "you",
    "authnet_save_customer_profile": "auto",
    "authnet_save_profile_from_transaction": "you",
    "authnet_save_shipping_address": "auto",
    "authnet_save_webhook": "you",
    "authnet_search_lookups": "auto",
    "authnet_send_receipt": "you",
    "authnet_set_test_mode": "you",
    "authnet_update_split_tender": "you",
    "authnet_update_subscription": "you",
    "authnet_validate_payment_profile": "you",
    "authnet_void_transaction": "you",
}

# gateway slug -> (tool prefix, approvals)
SERVERS = {
    "acceptblue": ("ablue_", ACCEPTBLUE_APPROVALS),
    "authnet": ("authnet_", AUTHNET_APPROVALS),
}


def auto_tools(approvals):
    return sorted(name for name, level in approvals.items() if level == "auto")


def person_tools(approvals):
    return sorted(name for name, level in approvals.items() if level == "you")
