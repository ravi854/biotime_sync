import frappe
from frappe.utils import now_datetime
from frappe.utils.data import time_diff_in_seconds

def process_biometric_job_card(doc, method=None):
    employee_id = doc.employee
    current_time = doc.time or now_datetime()

    # --- 1. ACCIDENTAL DOUBLE-PUNCH CHECK --- #
    last_checkin = frappe.db.get_value(
        "Employee Checkin",
        {
            "employee": employee_id,
            "name": ["!=", doc.name]
        },
        ["time"],
        order_by="time desc"
    )

    if last_checkin:
        time_gap = time_diff_in_seconds(current_time, last_checkin)
        if time_gap < 120:
            frappe.msgprint(f"Duplicate punch ignored for {employee_id}. Time gap: {time_gap}s.")
            return

    # --- 2. FIND OPEN JOB CARD ---
    # --- SEARCH FOR ACTIVE JOB CARD ---
    # Look for direct assignment first

 # This finds the parent Job Card names where this employee is listed
    assigned_job_cards = frappe.get_all(
        "Job Card Employee",
        filters={"employee": employee_id},
        pluck="parent"
    )

    if not assigned_job_cards:
        # Worker isn't assigned to any Job Cards; exit cleanly
        return

    # 3. Find if any of those assigned Job Cards are currently open or in progress
    job_card_name = frappe.db.get_value(
        "Job Card",
        {
            "name": ["in", assigned_job_cards],
            "status": ["in", ["Open", "Work In Progress"]],
            "docstatus": 1
        },
        "name"
    )

    # If no open job card matches their assignment, stop here
    if not job_card_name:
        return


    # --- 3. MANAGE TIMERS ---
    job_card = frappe.get_doc("Job Card", job_card_name)
    is_timer_running = False

    for log in job_card.get("time_logs"):
        if log.employee == employee_id and not log.to_time:
            log.to_time = current_time
            is_timer_running = True
            break

    if not is_timer_running:
        job_card.append("time_logs", {
            "employee": employee_id,
            "from_time": current_time
        })

    if job_card.status == "Open":
        job_card.status = "Work In Progress"

    # --- 4. SAVE CHANGES ---
    job_card.flags.ignore_permissions = True
    job_card.save()
