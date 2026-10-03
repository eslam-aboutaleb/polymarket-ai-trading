import unittest
from pathlib import Path

from sqlalchemy import DateTime

from app.models import Base
from app.utils.time import utc_now

BACKEND_DIR = Path(__file__).resolve().parents[1]


class TimezoneStandardizationTests(unittest.TestCase):
    def test_utc_now_is_timezone_aware(self):
        now = utc_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())

    def test_no_datetime_utcnow_usage_in_backend_app(self):
        app_dir = BACKEND_DIR / "app"
        offenders: list[str] = []
        for file_path in app_dir.rglob("*.py"):
            if "__pycache__" in file_path.parts:
                continue
            text = file_path.read_text(encoding="utf-8")
            if "datetime.utcnow" in text:
                offenders.append(str(file_path.relative_to(BACKEND_DIR)))
        self.assertEqual([], offenders, msg=f"datetime.utcnow found in: {offenders}")

    def test_model_datetime_columns_are_timezone_aware(self):
        offenders: list[str] = []
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, DateTime) and not column.type.timezone:
                    offenders.append(f"{table.name}.{column.name}")
        self.assertEqual(
            [],
            offenders,
            msg=f"Naive DateTime columns found: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
