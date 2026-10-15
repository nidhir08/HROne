from datetime import datetime, timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from fastapi import HTTPException

from app import main


class BusinessRuleTests(unittest.TestCase):
    def test_late_minutes_uses_attendance_date_for_overnight_tail(self):
        punch_in = datetime(2024, 1, 2, 5, 30, tzinfo=main.IST)

        late_minutes = main._compute_late_minutes(
            punch_in, "22:00", "2024-01-01"
        )

        self.assertEqual(late_minutes, 450)

    def test_late_minutes_grace_boundary_is_strict(self):
        on_boundary = datetime(2024, 1, 1, 9, 40, tzinfo=main.IST)
        one_second_late = on_boundary + timedelta(seconds=1)

        self.assertEqual(
            main._compute_late_minutes(on_boundary, "09:30", "2024-01-01"),
            0,
        )
        self.assertEqual(
            main._compute_late_minutes(one_second_late, "09:30", "2024-01-01"),
            10,
        )

    def test_work_hours_round_half_up_from_exact_seconds(self):
        punch_in = datetime(2024, 1, 1, tzinfo=main.IST)
        punch_out = punch_in + timedelta(seconds=9162)

        self.assertEqual(main._compute_work_hours(punch_in, punch_out), 2.55)

    def test_shared_rounding_helper_handles_float_tie_values(self):
        self.assertEqual(main._round_half_up(1.005, 2), 1.01)

    def test_joined_on_must_be_a_real_iso_date(self):
        payload = {
            "emp_code": "EMP1234",
            "name": "Test Employee",
            "email": "employee@example.com",
            "department": "Engineering",
            "joined_on": "2024-02-30",
        }

        with self.assertRaises(ValueError):
            main.EmployeeIn.model_validate(payload)

    def test_month_bounds_reject_year_zero(self):
        with self.assertRaises(HTTPException) as raised:
            main._month_bounds("0000-01")

        self.assertEqual(raised.exception.status_code, 422)

    def test_month_bounds_support_last_representable_month(self):
        self.assertEqual(
            main._month_bounds("9999-12"),
            ("9999-12-01", "9999-12-31"),
        )

    def test_department_filter_runs_after_employee_lookup(self):
        headcounts = [
            {
                "_id": "Engineering",
                "headcount": 1,
                "emp_codes": ["EMP1234"],
            }
        ]
        employees = SimpleNamespace(aggregate=Mock(return_value=headcounts))
        logs = SimpleNamespace(aggregate=Mock(return_value=[]))
        fake_db = SimpleNamespace(employees=employees, attendance_logs=logs)
        with patch.object(main, "db", fake_db):
            result = main.department_summary("2024-01", "Engineering")

        pipeline = logs.aggregate.call_args.args[0]
        self.assertEqual(pipeline[1]["$lookup"]["as"], "emp_info")
        self.assertEqual(pipeline[2], {"$unwind": "$emp_info"})
        self.assertEqual(
            pipeline[3], {"$match": {"emp_info.department": "Engineering"}}
        )
        self.assertEqual(result["items"][0]["department"], "Engineering")


if __name__ == "__main__":
    unittest.main()
