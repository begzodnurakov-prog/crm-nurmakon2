from calendar import monthrange
from datetime import date, datetime, time

from sqlalchemy import inspect, text

from extensions import db
from models import Attendance, ClassSession, Group, Payment, Student


def initialize_database(app):
    with app.app_context():
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        if 'attendance' in tables and 'legacy_attendance' not in tables:
            with db.engine.begin() as connection:
                connection.execute(text('ALTER TABLE attendance RENAME TO legacy_attendance'))

        db.create_all()
        _add_legacy_columns()
        _create_search_indexes()
        _assign_legacy_groups()
        _migrate_legacy_payments()
        _migrate_legacy_attendance()
        db.session.commit()


def _add_legacy_columns():
    inspector = inspect(db.engine)
    student_columns = {column['name'] for column in inspector.get_columns('student')}
    payment_columns = {column['name'] for column in inspector.get_columns('payment')}

    student_additions = {
        'enrollment_date': 'DATE',
        'status': "VARCHAR(20) NOT NULL DEFAULT 'active'",
        'group_id': 'INTEGER REFERENCES learning_group(id) ON DELETE SET NULL',
    }
    payment_additions = {
        'due_date': 'DATE',
        'paid_at': 'DATETIME',
        'created_at': 'DATETIME',
    }

    with db.engine.begin() as connection:
        for name, sql_type in student_additions.items():
            if name not in student_columns:
                connection.execute(text(f'ALTER TABLE student ADD COLUMN {name} {sql_type}'))
        for name, sql_type in payment_additions.items():
            if name not in payment_columns:
                connection.execute(text(f'ALTER TABLE payment ADD COLUMN {name} {sql_type}'))
        connection.execute(text('UPDATE student SET enrollment_date = CURRENT_DATE WHERE enrollment_date IS NULL'))
        connection.execute(text("UPDATE student SET status = 'active' WHERE status IS NULL OR status = ''"))
        connection.execute(text('UPDATE payment SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL'))


def _create_search_indexes():
    statements = (
        'CREATE INDEX IF NOT EXISTS ix_student_name ON student (name)',
        'CREATE INDEX IF NOT EXISTS ix_student_phone ON student (phone)',
        'CREATE INDEX IF NOT EXISTS ix_student_group_id ON student (group_id)',
        'CREATE INDEX IF NOT EXISTS ix_payment_month ON payment (month)',
        'CREATE INDEX IF NOT EXISTS ix_payment_status ON payment (status)',
        'CREATE INDEX IF NOT EXISTS ix_payment_due_date ON payment (due_date)',
        'CREATE INDEX IF NOT EXISTS ix_payment_student_id ON payment (student_id)',
    )
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _assign_legacy_groups():
    students = Student.query.filter(Student.group_id.is_(None)).all()
    courses = sorted({student.course.strip() or 'General' for student in students})
    groups_by_course = {}

    for course in courses:
        group_name = f'Legacy - {course}'[:100]
        group = Group.query.filter_by(name=group_name).first()
        if group is None:
            group = Group(name=group_name, course_name=course, schedule='Imported legacy group')
            db.session.add(group)
            db.session.flush()
        groups_by_course[course] = group

    for student in students:
        course = student.course.strip() or 'General'
        student.group = groups_by_course[course]
        student.course = course


def _migrate_legacy_payments():
    for payment in Payment.query.filter_by(status='unpaid').all():
        payment.status = 'pending'
        if payment.month:
            try:
                month_start = datetime.strptime(payment.month, '%Y-%m').date()
                last_day = monthrange(month_start.year, month_start.month)[1]
                payment.due_date = date(month_start.year, month_start.month, last_day)
            except ValueError:
                payment.due_date = None


def _migrate_legacy_attendance():
    tables = set(inspect(db.engine).get_table_names())
    if 'legacy_attendance' not in tables:
        return

    with db.engine.connect() as connection:
        rows = connection.execute(text(
            'SELECT id, student_id, attendance_date, status FROM legacy_attendance ORDER BY id'
        )).mappings().all()

    sessions = {}
    for row in rows:
        student = db.session.get(Student, row['student_id'])
        if student is None:
            continue
        legacy_date = row['attendance_date']
        if isinstance(legacy_date, str):
            legacy_date = date.fromisoformat(legacy_date[:10])
        session_key = (student.group_id, legacy_date)
        session = sessions.get(session_key)
        if session is None:
            session = ClassSession.query.filter_by(
                group_id=student.group_id,
                starts_at=datetime.combine(legacy_date, time.min),
                topic='Migrated daily attendance',
            ).first()
            if session is None:
                session = ClassSession(
                    group_id=student.group_id,
                    starts_at=datetime.combine(legacy_date, time.min),
                    topic='Migrated daily attendance',
                )
                db.session.add(session)
                db.session.flush()
            sessions[session_key] = session

        if Attendance.query.filter_by(student_id=student.id, session_id=session.id).first():
            continue
        status = row['status'] if row['status'] in ('present', 'absent', 'late') else 'absent'
        db.session.add(Attendance(student_id=student.id, session_id=session.id, status=status))