import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from extensions import db
from models import Attendance, ClassSession, Payment, Student
from schema_bootstrap import initialize_database


class MigrationConfig:
    TESTING = True
    SECRET_KEY = 'migration-test-secret'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class LegacyMigrationTests(unittest.TestCase):
    def test_existing_student_payment_and_daily_attendance_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / 'legacy.db'
            connection = sqlite3.connect(database_path)
            connection.executescript("""
                CREATE TABLE student (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    course VARCHAR(100) NOT NULL,
                    phone VARCHAR(20) NOT NULL
                );
                CREATE TABLE payment (
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    month VARCHAR(7) NOT NULL,
                    status VARCHAR(20) NOT NULL
                );
                CREATE TABLE attendance (
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL,
                    attendance_date DATE NOT NULL,
                    status VARCHAR(10) NOT NULL,
                    CONSTRAINT uq_attendance_student_date UNIQUE (student_id, attendance_date)
                );
                INSERT INTO student VALUES (7, 'Legacy Learner', 'Mathematics', '+998901234567');
                INSERT INTO payment VALUES (11, 7, 250000, '2026-08', 'unpaid');
                INSERT INTO attendance VALUES (13, 7, '2026-08-12', 'present');
            """)
            connection.commit()
            connection.close()

            config = type('TestMigrationConfig', (MigrationConfig,), {
                'SQLALCHEMY_DATABASE_URI': f"sqlite:///{database_path.as_posix()}",
            })
            migration_app = create_app(config)
            def dispose_engine():
                with migration_app.app_context():
                    db.session.remove()
                    db.engine.dispose()

            self.addCleanup(dispose_engine)
            initialize_database(migration_app)

            with migration_app.app_context():
                learner = db.session.get(Student, 7)
                self.assertEqual(learner.name, 'Legacy Learner')
                self.assertIsNotNone(learner.group_id)
                legacy_payment = Payment.query.filter_by(id=11).one()
                self.assertEqual(legacy_payment.status, 'pending')
                self.assertEqual(legacy_payment.due_date.isoformat(), '2026-08-31')
                self.assertEqual(db.session.execute(db.text(
                    'SELECT COUNT(*) FROM legacy_attendance WHERE id = 13'
                )).scalar_one(), 1)
                record = Attendance.query.join(ClassSession).filter(
                    Attendance.student_id == 7,
                    ClassSession.topic == 'Migrated daily attendance',
                ).one()
                self.assertEqual(record.status, 'present')
                self.assertEqual(record.session.starts_at.date().isoformat(), '2026-08-12')
                initialize_database(migration_app)
                self.assertEqual(Attendance.query.filter_by(student_id=7).count(), 1)
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    unittest.main()