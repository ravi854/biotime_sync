import requests
import frappe
from frappe import _
from frappe.model.document import Document
from datetime import datetime, timedelta
from frappe.utils import get_datetime, get_time, now_datetime, today, flt
from frappe.utils.synchronization import filelock
import json

def checkin_exists(employee, punch_dt):
    # Treat any punch within the same minute as duplicate
    start = punch_dt.replace(second=0, microsecond=0)
    end = start + timedelta(minutes=1)

    return frappe.db.exists(
        "Employee Checkin",
        {
            "employee": employee,
            "device_id": "BioTime",
            "time": ["between", [start, end]],
        },
    )


def time_diff_in_minutes(time1, time2):
    dt1 = datetime.combine(datetime.today(), time1)
    dt2 = datetime.combine(datetime.today(), time2)
    return abs((dt1 - dt2).total_seconds()) / 60


def get_shift_info(employee):
    sa = frappe.get_all(
        "Shift Assignment",
        filters={"employee": employee, "docstatus": 1},
        fields=["shift_type"],
        order_by="start_date desc",
        limit=1,
    )
    if sa:
        return sa[0].shift_type

    return frappe.db.get_value("Employee", employee, "default_shift")


def get_log_type(employee, punch_dt, punch_state_display):
    shift_type = get_shift_info(employee)

    if not shift_type:
        return "IN" if punch_state_display == "Check In" else "OUT"

    shift = frappe.get_doc("Shift Type", shift_type)

    start = get_time(shift.start_time)
    end = get_time(shift.end_time)
    late_grace = int(shift.late_entry_grace_period or 0)
    early_grace = int(shift.early_exit_grace_period or 0)

    punch_time = punch_dt.time()

    if punch_state_display == "Check In":
        if punch_time > start and time_diff_in_minutes(punch_time, start) > late_grace:
            return "Late Entry"
        return "IN"

    if punch_state_display == "Check Out":
        if punch_time < end and time_diff_in_minutes(end, punch_time) > early_grace:
            return "Early Exit"
        return "OUT"

    return "IN"





#

def process_simple_checkin(transaction):

    try:
        emp_code = transaction.get("emp_code")
        punch_time = transaction.get("punch_time")
        punch_state = transaction.get("punch_state_display")
        device_id = transaction.get('terminal_alias') or transaction.get('terminal_sn')
        transaction_id = transaction.get('id')
        area_alias = transaction.get("area_alias") or None


        if not (emp_code or punch_time):
            frappe.log_error(f"Missing required fields in transaction: {transaction}", "ZKTeco Transaction Error")
            return False

    #punch_dt = get_datetime(punch_time)

        employee = find_employee_by_code(emp_code)

    #employee = frappe.db.get_value(
     #   "Employee",
      #  {"biotime_emp_code": emp_code},
       # "name",
   # )
        if not employee:
            frappe.log_error(f"Employee not found for code: {emp_code}", "ZKTeco Employee Mapping")
            return "skipped"

        if isinstance(punch_time, str):
            punch_datetime = get_datetime(punch_time)
        else:
            punch_datetime = punch_time

        log_type = "IN"

        if punch_state == "1":  # Based on API response: "1" = Check Out
            log_type = "OUT"

        existing_checkin = frappe.db.exists("Employee Checkin", {
            "employee": employee,
            "time": punch_datetime,
            "device_id": device_id
        })

        if existing_checkin:
            return True

        if transaction_id:
            existing_by_id = frappe.db.get_value("Employee Checkin",
                                               {"device_id": device_id, "employee": employee},
                                               "name",
                                               {"time": ["between", [punch_datetime - timedelta(seconds=5), punch_datetime + timedelta(seconds=5)]]})
            if existing_by_id:
                return True

    #log_type = "IN" if punch_state == "Check In" else "OUT"

        checkin = frappe.get_doc({

            "doctype": "Employee Checkin",
            "employee": employee,
            "time": punch_datetime,
            "log_type": log_type,
            "device_id": "BioTime",
            "custom_location_id": area_alias,
            "skip_auto_attendance": 0
        })
        checkin.insert(ignore_permissions=True)
        frappe.db.commit()

        return True

    except Exception as e:
           frappe.log_error(f"Error creating Employee Checkin: {str(e)}", "ZKTeco Checkin Creation")
           return False


def find_employee_by_code(emp_code):
    employee = frappe.db.get_value("Employee", {"custom_biotime_emp_code": emp_code}, "name")
    if employee:
        return employee

    #employee = frappe.db.get_value("Employee", {"user_id": emp_code}, "name")
    #if employee:
     #   return employee

    if frappe.db.has_column("Employee", "attendance_device_id"):
        employee = frappe.db.get_value("Employee", {"attendance_device_id": emp_code}, "name")
        if employee:
            return employee

    return None

@frappe.whitelist()
def manual_sync():

    try:
        run_biotime_attendance()
        return {"success": True, "message": "Sync completed successfully"}
    except Exception as e:
        frappe.log_error(f"Manual sync failed: {str(e)}", "ZKTeco Manual Sync")
        return {"success": False, "message": f"Sync failed: {str(e)}"}



def scheduled_sync():
    try:
        cfg = frappe.get_single("BioTime Settings")
        if not cfg.enable_sync:
            return

        # For frequent syncs (less than 60 seconds), check if we should actually run
        sync_seconds = int(cfg.seconds or 300)
        if sync_seconds < 60:
            last_run = frappe.cache().get_value("zkteco_last_sync_run")
            current_time = now_datetime()

            if last_run:
                time_diff = (current_time - get_datetime(last_run)).total_seconds()
                if time_diff < sync_seconds:
                    return  # Not yet time for next sync

            # Update last run time
            frappe.cache().set_value("zkteco_last_sync_run", current_time)

        run_biotime_attendance()

    except Exception as e:
        frappe.log_error(f"Scheduled ZKTeco sync failed: {str(e)}", "ZKTeco Scheduled Sync Error")


def cleanup_scheduler_check():

    try:
        cfg = frappe.get_single("BioTime Settings")
        if cfg.enable_sync:
            # Log that the scheduler is active
            frappe.logger().info("ZKTeco scheduler check: Active")
    except Exception as e:
        frappe.log_error(f"ZKTeco scheduler check failed: {str(e)}", "ZKTeco Scheduler Check")


@frappe.whitelist()
def get_sync_status():

    try:
        cfg = frappe.get_single("BioTime Settings")

        # Get last sync time
        last_sync = frappe.db.get_single_value("BioTime Settings", "last_synced_datetime")

        # Count recent employee checkins from ZKTeco
        recent_checkins = frappe.db.count("Employee Checkin", {
            "device_id": ["like", "%ZKTeco%"],
            "creation": [">=", frappe.utils.add_days(today(), -1)]
        })

        return {
            "enabled": cfg.enable_sync,
            "sync_frequency": cfg.seconds,
            "last_sync": last_sync,
            "recent_checkins_24h": recent_checkins,
            "server_configured": bool(cfg.biotime_url),
            "token_configured": bool(cfg.biotime_token)
        }

    except Exception as e:
        return {"error": str(e)}


def process_shift_based_checkin(row):
    emp_code = row.get("emp_code")
    punch_time = row.get("punch_time")
    punch_state = row.get("punch_state_display")

    if not (emp_code and punch_time and punch_state):
        return "skipped"

    punch_dt = get_datetime(punch_time)

    employee = frappe.db.get_value(
        "Employee",
        {"custom_biotime_emp_code": emp_code},
        "name",
    )
    if not employee:
        return "skipped"

    if checkin_exists(employee, punch_dt):
        return "skipped"

    log_type = get_log_type(employee, punch_dt, punch_state)

    frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": employee,
            "time": punch_dt,
            "log_type": log_type,
            "device_id": "BioTime",
        }
    ).insert(ignore_permissions=True)

    return "inserted"


# ---------------------------------------------------------


@frappe.whitelist()
def biotime_attendance():
    frappe.enqueue(
        "biotime_sync.attendance.run_biotime_attendance",
        queue="long",
        job_name="BioTime Datetime Sync",
    )
    return {"message": "BioTime syncing started"}




def run_biotime_attendance():

    cfg = frappe.get_single("BioTime Settings")
    if not cfg.enable_sync:
        frappe.log_error("ZKTeco sync is disabled", "ZKTeco Sync")
        return

    if not cfg.biotime_token:
        frappe.log_error("ZKTeco token not configured", "ZKTeco Sync")
        return

    try:
        last_synced_datetime = frappe.db.get_single_value("BioTime Settings", "last_synced_datetime") or (now_datetime() - timedelta(hours=1))
        current_time = now_datetime()

        transactions = fetch_zkteco_transactions(cfg, last_synced_datetime, current_time)

        if transactions:
            processed_count = 0
            error_count = 0

            for transaction in transactions:
                try:
                    if process_simple_checkin(transaction):
                        processed_count += 1
                    else:
                        error_count += 1
                except Exception as e:
                    error_count += 1
                    frappe.log_error(f"Error creating checkin for transaction {transaction}: {str(e)}", "ZKTeco Sync Error")


                total_synced = frappe.db.get_single_value("BioTime Settings", "total_synced_records") or 0
                frappe.db.set_single_value("BioTime Settings", "last_synced_datetime", current_time)
                frappe.db.set_single_value("BioTime Settings", "total_synced_records", total_synced + processed_count)

                frappe.db.commit()

                frappe.logger().info(f"ZKTeco Sync completed: {processed_count} processed, {error_count} errors")

    except Exception as e:
            frappe.log_error(f"ZKTeco sync failed: {str(e)}", "ZKTeco Sync Fatal Error")


def fetch_zkteco_transactions(cfg, start_time, end_time):
    base_url = cfg.biotime_url.rstrip("/") + "/iclock/api/transactions/"
    headers = {"Authorization": f"Token {cfg.biotime_token}"}
    params = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    try:
        response = requests.get(base_url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        if isinstance(data, dict) and 'data' in data:
            return data['data']
        elif isinstance(data, dict) and 'results' in data:
            return data['results']
        elif isinstance(data, list):
            return data
        else:
            return []

    except Exception as e:
        frappe.log_error(f"Failed to fetch ZKTeco transactions: {str(e)}", "ZKTeco API Error")
        return []


