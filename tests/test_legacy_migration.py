import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from app import create_app
from extensions import db
from flask_migrate import downgrade, upgrade
from models import Attendance, ClassSession, Course, Payment, Student
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
                self.assertEqual(learner.groups[0].course.title, 'Mathematics')
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

    def test_alembic_upgrade_backfills_courses_and_group_memberships(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / 'alembic.db'
            config = type('AlembicTestConfig', (MigrationConfig,), {
                'SQLALCHEMY_DATABASE_URI': f"sqlite:///{database_path.as_posix()}",
            })
            migration_app = create_app(config)
            with migration_app.app_context():
                try:
                    upgrade(directory='migrations', revision='c43b6a12e8f1')
                    with db.engine.begin() as connection:
                        connection.execute(db.text(
                            "INSERT INTO user_account (id, full_name, email, password_hash, role, enabled, created_at) "
                            "VALUES (1, 'Teacher', 'teacher@example.test', 'unused', 'teacher', 1, CURRENT_TIMESTAMP)"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO learning_group (id, name, course_name, teacher_id, schedule, is_active, created_at) "
                            "VALUES (2, 'Algebra A', 'Algebra', 1, 'Mon/Wed 18:00', 1, CURRENT_TIMESTAMP)"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO student (id, name, course, phone, enrollment_date, status, balance, payment_status, group_id) "
                            "VALUES (3, 'Learner', 'Algebra', '+998901234567', CURRENT_DATE, 'active', 250000, 'pending', 2)"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO payment (id, student_id, amount, month, status, due_date, created_at, payment_type) "
                            "VALUES (6, 3, 250000, '2026-09', 'pending', '2026-10-05', CURRENT_TIMESTAMP, 'cash')"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO payment (id, student_id, amount, month, status, paid_at, created_at, payment_type, payment_date) "
                            "VALUES (7, 3, 90000, '2026-09', 'paid', '2026-09-26 12:00:00', CURRENT_TIMESTAMP, 'cash', '2026-09-26')"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO class_session (id, group_id, starts_at, topic, created_by_id) "
                            "VALUES (4, 2, '2026-09-26 18:00:00', 'Math', 1)"
                        ))
                        connection.execute(db.text(
                            "INSERT INTO attendance_record (id, student_id, session_id, status, marked_by_id, marked_at) "
                            "VALUES (5, 3, 4, 'late', 1, '2026-09-26 18:30:00')"
                        ))
                    upgrade(directory='migrations')
                    student = db.session.get(Student, 3)
                    migrated_payment = db.session.get(Payment, 7)
                    course = Course.query.filter_by(title='Algebra').one()
                    existing_attendance = db.session.get(Attendance, 5)
                    self.assertEqual(student.group_id, 2)
                    self.assertEqual(student.balance, -250000)
                    self.assertIsInstance(migrated_payment.payment_date, datetime)
                    self.assertEqual(migrated_payment.payment_date.date(), date(2026, 9, 26))
                    self.assertFalse(migrated_payment.is_credit)
                    self.assertEqual([group.id for group in student.groups], [2])
                    self.assertEqual(student.groups[0].course_id, course.id)
                    self.assertEqual(existing_attendance.group_id, 2)
                    self.assertEqual(existing_attendance.session_date.isoformat(), '2026-09-26')
                    daily = Attendance(
                        student_id=3, group_id=2, session_date=date(2026, 9, 27),
                        session_id=None, status='excused',
                    )
                    db.session.add(daily)
                    db.session.commit()
                    daily_id = daily.id
                    db.session.remove()
                    downgrade(directory='migrations', revision='d21a6f74c308')
                    with db.engine.connect() as connection:
                        archived = connection.execute(db.text(
                            'SELECT session_id, status FROM attendance_record WHERE id = :id'
                        ), {'id': daily_id}).one()
                        self.assertIsNotNone(archived.session_id)
                        self.assertEqual(archived.status, 'absent')
                finally:
                    db.session.remove()
                    db.engine.dispose()


if __name__ == '__main__':
    unittest.main()