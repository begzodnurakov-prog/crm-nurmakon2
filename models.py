from datetime import date, datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db

parent_students = db.Table(
    'parent_students',
    db.Column('user_id', db.Integer, db.ForeignKey('user_account.id', ondelete='CASCADE'), primary_key=True),
    db.Column('student_id', db.Integer, db.ForeignKey('student.id', ondelete='CASCADE'), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = 'user_account'
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'teacher', 'student_parent')", name='ck_user_role'),
    )

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(24), nullable=False, default='student_parent')
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    teaching_groups = db.relationship('Group', back_populates='teacher', foreign_keys='Group.teacher_id')
    students = db.relationship('Student', secondary=parent_students, back_populates='parents')
    notifications = db.relationship('Notification', back_populates='user', cascade='all, delete-orphan')
    inquiries = db.relationship('Inquiry', back_populates='sender')

    @property
    def is_active(self):
        return self.enabled

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Student(db.Model):
    __tablename__ = 'student'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    course = db.Column(db.String(100), nullable=False, default='Unassigned')
    phone = db.Column(db.String(20), nullable=False, index=True)
    enrollment_date = db.Column(db.Date, nullable=True, default=date.today)
    status = db.Column(db.String(20), nullable=False, default='active')
    group_id = db.Column(db.Integer, db.ForeignKey('learning_group.id'), nullable=True, index=True)

    group = db.relationship('Group', back_populates='students')
    parents = db.relationship('User', secondary=parent_students, back_populates='students')
    payments = db.relationship('Payment', back_populates='student', cascade='all, delete-orphan')
    attendance_records = db.relationship('Attendance', back_populates='student', cascade='all, delete-orphan')


class Group(db.Model):
    __tablename__ = 'learning_group'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    course_name = db.Column(db.String(100), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user_account.id', ondelete='SET NULL'), nullable=True, index=True)
    schedule = db.Column(db.String(250), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    teacher = db.relationship('User', back_populates='teaching_groups', foreign_keys=[teacher_id])
    students = db.relationship('Student', back_populates='group')
    sessions = db.relationship('ClassSession', back_populates='group', cascade='all, delete-orphan')


class ClassSession(db.Model):
    __tablename__ = 'class_session'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('learning_group.id', ondelete='CASCADE'), nullable=False, index=True)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    topic = db.Column(db.String(200), nullable=False, default='')
    created_by_id = db.Column(db.Integer, db.ForeignKey('user_account.id', ondelete='SET NULL'), nullable=True)

    group = db.relationship('Group', back_populates='sessions')
    attendances = db.relationship('Attendance', back_populates='session', cascade='all, delete-orphan')


class Attendance(db.Model):
    __tablename__ = 'attendance_record'
    __table_args__ = (
        db.UniqueConstraint('student_id', 'session_id', name='uq_attendance_student_session'),
        CheckConstraint("status IN ('present', 'absent', 'late')", name='ck_attendance_status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('class_session.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(10), nullable=False)
    marked_by_id = db.Column(db.Integer, db.ForeignKey('user_account.id', ondelete='SET NULL'), nullable=True)
    marked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    student = db.relationship('Student', back_populates='attendance_records')
    session = db.relationship('ClassSession', back_populates='attendances')


class Payment(db.Model):
    __tablename__ = 'payment'
    __table_args__ = (
        CheckConstraint("status IN ('paid', 'pending')", name='ck_payment_status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    month = db.Column(db.String(7), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    due_date = db.Column(db.Date, nullable=True, index=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=True, default=lambda: datetime.now(timezone.utc))

    student = db.relationship('Student', back_populates='payments')

    @property
    def effective_status(self):
        if self.status == 'paid':
            return 'paid'
        if self.due_date and self.due_date < date.today():
            return 'overdue'
        return 'pending'


class LegacyAttendance(db.Model):
    __tablename__ = 'legacy_attendance'
    __table_args__ = (
        db.UniqueConstraint('student_id', 'attendance_date', name='uq_attendance_student_date'),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(10), nullable=False)


class Notification(db.Model):
    __tablename__ = 'notification'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'dedupe_key', name='uq_notification_user_dedupe'),
        CheckConstraint(
            "kind IN ('payment_due', 'class_reminder', 'attendance_update', 'support_reply')",
            name='ck_notification_kind',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_account.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.String(1000), nullable=False)
    target_url = db.Column(db.String(255), nullable=True)
    dedupe_key = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship('User', back_populates='notifications')


class Inquiry(db.Model):
    __tablename__ = 'support_inquiry'
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved')",
            name='ck_inquiry_status',
        ),
        CheckConstraint(
            "category IN ('general', 'payment', 'attendance', 'technical')",
            name='ck_inquiry_category',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user_account.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id', ondelete='SET NULL'), nullable=True, index=True)
    category = db.Column(db.String(20), nullable=False, default='general')
    subject = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    admin_response = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    sender = db.relationship('User', back_populates='inquiries')
    student = db.relationship('Student')