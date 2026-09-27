import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urljoin, urlparse

import click
from email_validator import EmailNotValidError, validate_email
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_limiter.errors import RateLimitExceeded
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from config import Config
from extensions import csrf, db, limiter, login_manager, migrate
from models import Attendance, ClassSession, Course, Group, Inquiry, Notification, Payment, Student, User
from schema_bootstrap import initialize_database


ROLE_LABELS = {
    'admin': 'Administrator',
    'teacher': "O'qituvchi",
    'student_parent': "O'quvchi / ota-ona",
}
ATTENDANCE_STATUS_LABELS = {
    'present': 'Present',
    'absent': 'Absent',
    'late': 'Late',
    'excused': 'Excused',
}


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    if app.config.get('APP_ENV') == 'production':
        if len(os.environ.get('SECRET_KEY', '')) < 32:
            raise RuntimeError('Set a randomly generated SECRET_KEY of at least 32 characters in production.')
        if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:'):
            raise RuntimeError('Configure a managed DATABASE_URL for production.')
        if app.config['RATELIMIT_STORAGE_URI'].startswith('memory:'):
            raise RuntimeError('Configure shared Redis RATELIMIT_STORAGE_URI for production.')
    if app.config.get('TESTING'):
        app.config['WTF_CSRF_ENABLED'] = False

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Davom etish uchun tizimga kiring.'
    login_manager.login_message_category = 'warning'

    register_routes(app)
    register_cli(app)
    register_error_handlers(app)

    @app.context_processor
    def inject_unread_notification_count():
        count = 0
        open_inquiry_count = 0
        if current_user.is_authenticated:
            count = Notification.query.filter_by(user_id=current_user.id, read_at=None).count()
            if current_user.role == 'admin':
                open_inquiry_count = Inquiry.query.filter(Inquiry.status != 'resolved').count()
        return {
            'unread_notification_count': count,
            'open_inquiry_count': open_inquiry_count,
        }

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        if app.config.get('APP_ENV') == 'production':
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    return app


@login_manager.user_loader
def load_user(user_id):
    try:
        user = db.session.get(User, int(user_id))
        return user if user and user.enabled else None
    except (TypeError, ValueError):
        return None


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.url))
        if current_user.role != 'admin':
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _safe_next_url(target):
    if not target:
        return None
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    if redirect_url.scheme in ('http', 'https') and host_url.netloc == redirect_url.netloc:
        return target
    return None


def _student_scope_query(user):
    query = Student.query
    if user.role == 'teacher':
        query = query.filter(Student.groups.any(Group.teacher_id == user.id))
    elif user.role == 'student_parent':
        query = query.join(Student.parents).filter(User.id == user.id)
    return query


def _group_scope_query(user):
    query = Group.query
    if user.role == 'teacher':
        query = query.filter(Group.teacher_id == user.id)
    elif user.role == 'student_parent':
        query = query.join(Group.enrolled_students).join(Student.parents).filter(User.id == user.id)
    return query.distinct()


def _can_manage_group(user, group):
    return user.role == 'admin' or (user.role == 'teacher' and group.teacher_id == user.id)


def _parse_date(value):
    return datetime.strptime(value, '%Y-%m-%d').date()


def _parse_datetime_local(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M')


def _group_form_values(form):
    title = form.get('title', form.get('name', '')).strip()
    course_id = form.get('course_id', '').strip()
    course = db.session.get(Course, int(course_id)) if course_id else None
    course_name = form.get('course_name', '').strip()
    if course is None and course_name:
        course = Course.query.filter_by(title=course_name).first()
        if course is None:
            course = Course(title=course_name)
            db.session.add(course)
            db.session.flush()

    teacher_id = form.get('teacher_id', '').strip()
    teacher = db.session.get(User, int(teacher_id)) if teacher_id else None
    schedule_days = form.get('schedule_days', '').strip()
    start_time_text = form.get('start_time', '').strip()
    if schedule_days not in ('', 'Mon-Wed-Fri', 'Tue-Thu-Sat'):
        raise ValueError
    if bool(schedule_days) != bool(start_time_text):
        raise ValueError
    start_time = datetime.strptime(start_time_text, '%H:%M').time() if start_time_text else None
    schedule = f'{schedule_days} {start_time:%H:%M}' if start_time else form.get('schedule', '').strip()
    room_number = form.get('room_number', '').strip()
    if (
        not title or len(title) > 100 or course is None or len(course.title) > 100
        or (teacher_id and (teacher is None or teacher.role != 'teacher' or not teacher.enabled))
        or len(room_number) > 80 or len(schedule) > 250
    ):
        raise ValueError
    return {
        'title': title,
        'course': course,
        'teacher': teacher,
        'schedule_days': schedule_days,
        'start_time': start_time,
        'room_number': room_number,
        'schedule': schedule,
    }


def _attendance_roster_query(group_id):
    return Student.query.filter(
        Student.status == 'active',
        or_(
            Student.group_id == group_id,
            Student.groups.any(Group.id == group_id),
        ),
    )


def calculate_monthly_attendance_percentages(group_id, year, month):
    if month < 1 or month > 12:
        raise ValueError('month must be between 1 and 12')
    month_start = date(year, month, 1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    students = _attendance_roster_query(group_id).all()
    totals = {student.id: [0, 0] for student in students}
    records = Attendance.query.filter(
        Attendance.group_id == group_id,
        Attendance.session_date >= month_start,
        Attendance.session_date < next_month,
    ).all()
    daily_dates = {
        (record.student_id, record.session_date)
        for record in records if record.session_id is None
    }
    for record in records:
        if record.session_id is not None and (record.student_id, record.session_date) in daily_dates:
            continue
        if record.status == 'excused':
            continue
        total, attended = totals.setdefault(record.student_id, [0, 0])
        totals[record.student_id] = [total + 1, attended + (record.status in ('present', 'late'))]
    return {
        student_id: round(attended / total * 100, 1) if total else 0.0
        for student_id, (total, attended) in totals.items()
    }


def _course_form_values(form):
    title = form.get('title', '').strip()
    price = Decimal(form.get('price', ''))
    duration_months = int(form.get('duration_months', ''))
    if (
        not title or len(title) > 120 or not price.is_finite() or price < 0
        or duration_months < 1 or duration_months > 120
    ):
        raise ValueError
    return title, price, duration_months


def _sync_student_payment_state(student):
    pending_payments = Payment.query.filter_by(student_id=student.id, status='pending')
    student.balance = pending_payments.with_entities(
        func.coalesce(func.sum(Payment.amount), 0)
    ).scalar()
    has_overdue = pending_payments.filter(Payment.due_date < date.today()).first() is not None
    has_pending = pending_payments.first() is not None
    student.payment_status = 'overdue' if has_overdue else 'pending' if has_pending else 'paid'


def _linked_parent_ids(form):
    raw_ids = form.getlist('parent_ids')
    try:
        parent_ids = {int(value) for value in raw_ids}
    except ValueError:
        raise ValueError('Ota-ona tanlovini tekshiring.')
    parents = User.query.filter(
        User.id.in_(parent_ids),
        User.role == 'student_parent',
        User.enabled.is_(True),
    ).all() if parent_ids else []
    if len(parents) != len(parent_ids):
        raise ValueError("Tanlangan ota-ona hisoblari topilmadi.")
    return parents


def _create_notification(user, kind, title, message, dedupe_key, target_url):
    user_key = f'{dedupe_key}:user:{user.id}'
    existing = Notification.query.filter_by(user_id=user.id, dedupe_key=user_key).first()
    if existing:
        return False
    db.session.add(Notification(
        user_id=user.id,
        kind=kind,
        title=title,
        message=message[:1000],
        dedupe_key=user_key[:200],
        target_url=target_url,
    ))
    return True


def _notify_student_parents(student, kind, title, message, dedupe_key, target_url):
    return sum(
        _create_notification(parent, kind, title, message, dedupe_key, target_url)
        for parent in student.parents
        if parent.enabled and parent.role == 'student_parent'
    )


def register_routes(app):
    @app.get('/service-worker.js')
    def service_worker():
        response = send_from_directory(
            app.static_folder,
            'service-worker.js',
            mimetype='application/javascript',
            max_age=0,
        )
        response.headers['Service-Worker-Allowed'] = '/'
        response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit('5 per minute', methods=['POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            user = User.query.filter(func.lower(User.email) == email).first()
            if user and user.enabled and user.check_password(password):
                login_user(user, remember=request.form.get('remember') == 'on')
                flash('Tizimga muvaffaqiyatli kirdingiz.', 'success')
                return redirect(_safe_next_url(request.args.get('next')) or url_for('dashboard'))
            flash("Email yoki parol noto'g'ri.", 'error')
        return render_template('auth/login.html')

    @app.post('/logout')
    @login_required
    def logout():
        logout_user()
        flash('Tizimdan chiqdingiz.', 'success')
        return redirect(url_for('login'))

    @app.route('/register', methods=['GET', 'POST'])
    @app.route('/auth/register', methods=['GET', 'POST'])
    @csrf.exempt
    def disabled_registration():
        abort(403)

    @app.get('/')
    @login_required
    def dashboard():
        today = date.today()
        month_starts = []
        for months_ago in reversed(range(6)):
            month_index = today.year * 12 + today.month - 1 - months_ago
            month_starts.append(date(month_index // 12, month_index % 12 + 1, 1))
        next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        student_query = _student_scope_query(current_user)
        group_query = _group_scope_query(current_user)
        payment_query = Payment.query.join(Student)
        if current_user.role == 'teacher':
            payment_query = payment_query.filter(Student.groups.any(Group.teacher_id == current_user.id))
        elif current_user.role == 'student_parent':
            payment_query = payment_query.join(Student.parents).filter(User.id == current_user.id)

        income = payment_query.filter(
            Payment.status == 'paid',
            Payment.payment_date >= month_starts[-1],
            Payment.payment_date < next_month,
        ).with_entities(
            func.coalesce(func.sum(Payment.amount), 0)
        ).scalar() or 0
        revenue_rows = payment_query.filter(
            Payment.status == 'paid',
            Payment.payment_date >= month_starts[0],
            Payment.payment_date < next_month,
        ).with_entities(Payment.payment_date, Payment.amount).all()
        revenue_by_month = {month.strftime('%Y-%m'): 0 for month in month_starts}
        for payment_date, amount in revenue_rows:
            month_key = payment_date.strftime('%Y-%m')
            revenue_by_month[month_key] = round(revenue_by_month[month_key] + float(amount), 2)
        course_distribution = student_query.filter(Student.status == 'active').with_entities(
            Student.course, func.count(func.distinct(Student.id)),
        ).group_by(Student.course).order_by(Student.course).all()
        pending_query = payment_query.filter(Payment.status == 'pending')
        paid_count = payment_query.filter(Payment.status == 'paid').count()
        pending_count = pending_query.filter(or_(Payment.due_date.is_(None), Payment.due_date >= today)).count()
        overdue_count = pending_query.filter(Payment.due_date < today).count()
        debt_total = pending_query.with_entities(func.coalesce(func.sum(Payment.amount), 0)).scalar()
        debtor_count = pending_query.filter(Payment.due_date < today).with_entities(
            func.count(func.distinct(Payment.student_id))
        ).scalar() or 0
        notifications = Notification.query.filter_by(user_id=current_user.id).order_by(
            Notification.created_at.desc(), Notification.id.desc(),
        ).limit(6).all()
        upcoming_sessions = ClassSession.query.join(Group).filter(
            ClassSession.starts_at >= datetime.now()
        )
        if current_user.role == 'teacher':
            upcoming_sessions = upcoming_sessions.filter(Group.teacher_id == current_user.id)
        elif current_user.role == 'student_parent':
            upcoming_sessions = upcoming_sessions.join(Group.enrolled_students).join(Student.parents).filter(User.id == current_user.id)

        return render_template(
            'dashboard.html',
            student_count=student_query.filter(Student.status == 'active').distinct().count(),
            group_count=group_query.filter(Group.is_active.is_(True)).count(),
            monthly_income=income,
            debtor_count=debtor_count,
            revenue_labels=[month.strftime('%b %Y') for month in month_starts],
            revenue_values=[revenue_by_month[month.strftime('%Y-%m')] for month in month_starts],
            course_labels=[course or 'Unassigned' for course, _ in course_distribution],
            course_values=[count for _, count in course_distribution],
            paid_count=paid_count,
            pending_count=pending_count,
            overdue_count=overdue_count,
            debt_total=debt_total,
            notifications=notifications,
            unread_notification_count=Notification.query.filter_by(
                user_id=current_user.id, read_at=None,
            ).count(),
            students=student_query.order_by(Student.name).limit(8).all(),
            payments=payment_query.order_by(Payment.month.desc(), Payment.id.desc()).limit(8).all(),
            sessions=upcoming_sessions.order_by(ClassSession.starts_at).limit(6).all(),
            role_label=ROLE_LABELS.get(current_user.role, current_user.role),
            today_date=today.strftime('%d.%m.%Y'),
        )

    @app.get('/notifications')
    @login_required
    def notifications():
        page = db.paginate(
            Notification.query.filter_by(user_id=current_user.id).order_by(
                Notification.created_at.desc(), Notification.id.desc(),
            ).statement,
            per_page=30, max_per_page=100, error_out=False,
        )
        return render_template('notifications.html', notifications=page.items, page=page)

    @app.post('/notifications/<int:notification_id>/read')
    @login_required
    def mark_notification_read(notification_id):
        notification = Notification.query.filter_by(
            id=notification_id, user_id=current_user.id,
        ).first_or_404()
        if notification.read_at is None:
            notification.read_at = datetime.now()
            db.session.commit()
        target = notification.target_url or url_for('notifications')
        if not target.startswith('/') or target.startswith('//'):
            target = url_for('notifications')
        return redirect(target)

    @app.route('/contact', methods=['GET', 'POST'])
    @roles_required('student_parent')
    def contact_support():
        if request.method == 'POST':
            subject = request.form.get('subject', '').strip()
            message = request.form.get('message', '').strip()
            category = request.form.get('category', '')
            student_id = request.form.get('student_id', '').strip()
            student = None
            if student_id:
                try:
                    student = db.session.get(Student, int(student_id))
                except ValueError:
                    student = None
                if student not in current_user.students:
                    student = False

            if (
                len(subject) < 4 or len(subject) > 160
                or len(message) < 10 or len(message) > 5000
                or category not in ('general', 'payment', 'attendance', 'technical')
                or student is False
            ):
                flash("Xabar maydonlarini tekshiring va faqat o'zingizga bog'langan o'quvchini tanlang.", 'error')
            else:
                inquiry = Inquiry(
                    sender=current_user,
                    student=student,
                    category=category,
                    subject=subject,
                    message=message,
                )
                db.session.add(inquiry)
                db.session.commit()
                flash("Murojaatingiz yuborildi. Administrator javobini shu sahifada ko'rasiz.", 'success')
                return redirect(url_for('contact_support'))
        inquiries = Inquiry.query.filter_by(sender_id=current_user.id).order_by(
            Inquiry.created_at.desc(),
        ).limit(50).all()
        return render_template(
            'contact.html', students=current_user.students, inquiries=inquiries,
        )

    @app.get('/admin/inquiries')
    @roles_required('admin')
    def admin_inquiries():
        query = Inquiry.query
        status_filter = request.args.get('status', '').strip()
        if status_filter in ('open', 'in_progress', 'resolved'):
            query = query.filter(Inquiry.status == status_filter)
        search = request.args.get('q', '').strip()[:100]
        if search:
            pattern = f'%{search}%'
            query = query.join(User, Inquiry.sender_id == User.id).filter(
                or_(Inquiry.subject.ilike(pattern), Inquiry.message.ilike(pattern), User.full_name.ilike(pattern), User.email.ilike(pattern))
            )
        page = db.paginate(
            query.order_by(Inquiry.created_at.desc()).statement,
            per_page=25, max_per_page=100, error_out=False,
        )
        return render_template(
            'admin_inquiries.html', inquiries=page.items, page=page,
            status_filter=status_filter, search=search,
        )

    @app.post('/admin/inquiries/<int:inquiry_id>/update')
    @roles_required('admin')
    def update_inquiry(inquiry_id):
        inquiry = db.get_or_404(Inquiry, inquiry_id)
        status = request.form.get('status', '')
        response_text = request.form.get('admin_response', '').strip()
        if status not in ('open', 'in_progress', 'resolved') or len(response_text) > 5000:
            flash("Murojaat holati yoki javob matnini tekshiring.", 'error')
        else:
            has_new_response = bool(response_text and response_text != inquiry.admin_response)
            inquiry.status = status
            inquiry.admin_response = response_text
            inquiry.updated_at = datetime.now()
            if has_new_response:
                _create_notification(
                    inquiry.sender,
                    'support_reply',
                    'Murojaatingizga javob berildi',
                    f"{inquiry.subject}: {response_text}",
                    f'inquiry:{inquiry.id}:response:{inquiry.updated_at.isoformat()}',
                    url_for('contact_support'),
                )
            db.session.commit()
            flash('Murojaat yangilandi.', 'success')
        return redirect(url_for('admin_inquiries'))

    @app.route('/students', methods=['GET', 'POST'])
    @roles_required('admin')
    def students():
        groups = Group.query.filter_by(is_active=True).order_by(Group.name).all()
        parents = User.query.filter_by(role='student_parent', enabled=True).order_by(User.full_name).all()
        if request.method == 'POST':
            try:
                name = request.form.get('name', '').strip()
                phone = request.form.get('phone', '').strip()
                course = request.form.get('course', '').strip()
                enrollment_date = _parse_date(request.form.get('enrollment_date', ''))
                status = request.form.get('status', '')
                group_id = int(request.form['group_id'])
                group = db.session.get(Group, group_id)
                linked_parents = _linked_parent_ids(request.form)
                if (
                    not name or len(name) > 100 or not phone or len(phone) > 20
                    or not course or len(course) > 100 or group is None or not group.is_active
                ):
                    raise ValueError("Barcha majburiy maydonlarni to'ldiring.")
                if status not in ('active', 'paused', 'completed'):
                    raise ValueError("O'quvchi holati noto'g'ri.")
                student = Student(
                    name=name, phone=phone, course=course, enrollment_date=enrollment_date,
                    status=status, group=group, groups=[group], parents=linked_parents,
                )
                db.session.add(student)
                db.session.commit()
                flash("O'quvchi qo'shildi.", 'success')
                return redirect(url_for('students'))
            except (ValueError, KeyError):
                db.session.rollback()
                flash("O'quvchi ma'lumotlarini tekshiring.", 'error')
        query = Student.query.outerjoin(Student.group)
        search = request.args.get('q', '').strip()[:100]
        if search:
            pattern = f'%{search}%'
            query = query.filter(or_(Student.name.ilike(pattern), Student.phone.ilike(pattern)))
        student_page = db.paginate(
            query.order_by(Student.name).statement,
            per_page=25, max_per_page=100, error_out=False,
        )
        return render_template(
            'students.html', students=student_page.items, student_page=student_page,
            groups=groups, parents=parents, search=search, today_date=date.today().isoformat(),
        )

    @app.route('/students/<int:student_id>/edit', methods=['GET', 'POST'])
    @roles_required('admin')
    def edit_student(student_id):
        student = db.get_or_404(Student, student_id)
        groups = Group.query.filter_by(is_active=True).order_by(Group.name).all()
        parents = User.query.filter_by(role='student_parent', enabled=True).order_by(User.full_name).all()
        if request.method == 'POST':
            try:
                group = db.get_or_404(Group, int(request.form['group_id']))
                linked_parents = _linked_parent_ids(request.form)
                status = request.form.get('status', '')
                if status not in ('active', 'paused', 'completed') or not group.is_active:
                    raise ValueError
                student.name = request.form.get('name', '').strip()
                student.phone = request.form.get('phone', '').strip()
                student.course = request.form.get('course', '').strip()
                student.enrollment_date = _parse_date(request.form.get('enrollment_date', ''))
                student.status = status
                student.group = group
                if group not in student.groups:
                    student.groups.append(group)
                student.parents = linked_parents
                if (
                    not student.name or len(student.name) > 100
                    or not student.phone or len(student.phone) > 20
                    or not student.course or len(student.course) > 100
                ):
                    raise ValueError
                db.session.commit()
                flash("O'quvchi ma'lumotlari saqlandi.", 'success')
                return redirect(url_for('students'))
            except (ValueError, KeyError):
                db.session.rollback()
                flash("O'quvchi ma'lumotlarini tekshiring.", 'error')
        return render_template('student_form.html', student=student, groups=groups, parents=parents)

    @app.post('/students/<int:student_id>/delete')
    @roles_required('admin')
    def delete_student(student_id):
        student = db.get_or_404(Student, student_id)
        student.status = 'completed'
        db.session.commit()
        flash("O'quvchi arxivlandi. To'lov va davomat tarixi saqlandi.", 'success')
        return redirect(url_for('students'))

    @app.route('/courses', methods=['GET', 'POST'])
    @roles_required('admin')
    def courses():
        if request.method == 'POST':
            try:
                title, price, duration_months = _course_form_values(request.form)
                db.session.add(Course(title=title, price=price, duration_months=duration_months))
                db.session.commit()
                flash('Kurs yaratildi.', 'success')
                return redirect(url_for('courses'))
            except (ValueError, InvalidOperation, TypeError):
                db.session.rollback()
                flash('Kurs ma’lumotlarini tekshiring.', 'error')
            except IntegrityError:
                db.session.rollback()
                flash('Bu kurs nomi band.', 'error')
        return render_template('courses.html', courses=Course.query.order_by(Course.title).all())

    @app.route('/courses/<int:course_id>/edit', methods=['GET', 'POST'])
    @roles_required('admin')
    def edit_course(course_id):
        course = db.get_or_404(Course, course_id)
        if request.method == 'POST':
            try:
                title, price, duration_months = _course_form_values(request.form)
                course.title = title
                course.price = price
                course.duration_months = duration_months
                for group in course.groups:
                    group.course_name = title
                db.session.commit()
                flash('Kurs ma’lumotlari saqlandi.', 'success')
                return redirect(url_for('courses'))
            except (ValueError, InvalidOperation, TypeError):
                db.session.rollback()
                flash('Kurs ma’lumotlarini tekshiring.', 'error')
            except IntegrityError:
                db.session.rollback()
                flash('Bu kurs nomi band.', 'error')
        return render_template('course_form.html', course=course)

    @app.post('/courses/<int:course_id>/delete')
    @roles_required('admin')
    def delete_course(course_id):
        course = db.get_or_404(Course, course_id)
        if course.groups:
            flash('Avval ushbu kursga bog‘langan guruhlarni boshqa kursga o‘tkazing.', 'error')
        else:
            db.session.delete(course)
            db.session.commit()
            flash('Kurs o‘chirildi.', 'success')
        return redirect(url_for('courses'))

    @app.route('/groups', methods=['GET', 'POST'])
    @roles_required('admin')
    def groups():
        teachers = User.query.filter_by(role='teacher', enabled=True).order_by(User.full_name).all()
        courses = Course.query.order_by(Course.title).all()
        if request.method == 'POST':
            try:
                values = _group_form_values(request.form)
                db.session.add(Group(
                    title=values['title'], course_name=values['course'].title,
                    course=values['course'], teacher=values['teacher'], schedule=values['schedule'],
                    schedule_days=values['schedule_days'], start_time=values['start_time'],
                    room_number=values['room_number'],
                ))
                db.session.commit()
                flash('Guruh yaratildi.', 'success')
                return redirect(url_for('groups'))
            except (ValueError, TypeError):
                db.session.rollback()
                flash('Guruh ma’lumotlarini tekshiring.', 'error')
            except IntegrityError:
                db.session.rollback()
                flash('Bu guruh nomi band.', 'error')
        return render_template(
            'groups.html', groups=Group.query.order_by(Group.name).all(),
            teachers=teachers, courses=courses,
        )

    @app.get('/groups/<int:group_id>')
    @roles_required('admin', 'teacher')
    def group_detail(group_id):
        group = db.get_or_404(Group, group_id)
        if not _can_manage_group(current_user, group):
            abort(403)
        group_students = Student.query.filter(or_(
            Student.group_id == group.id,
            Student.groups.any(Group.id == group.id),
        )).order_by(Student.name).all()
        enrolled_ids = {student.id for student in group_students}
        available_students = [
            student for student in Student.query.filter_by(status='active').order_by(Student.name).all()
            if student.id not in enrolled_ids
        ]
        return render_template(
            'group_detail.html', group=group, group_students=group_students,
            available_students=available_students,
        )

    @app.route('/groups/<int:group_id>/attendance', methods=['GET', 'POST'])
    @roles_required('admin', 'teacher')
    def group_daily_attendance(group_id):
        group = db.get_or_404(Group, group_id)
        if not _can_manage_group(current_user, group):
            abort(403)
        try:
            attendance_date = _parse_date(request.values.get('date') or date.today().isoformat())
        except ValueError:
            flash('Davomat sanasini tekshiring.', 'error')
            return redirect(url_for('group_daily_attendance', group_id=group.id))
        if attendance_date > date.today():
            flash('Kelajak sanasi uchun davomat belgilab bo‘lmaydi.', 'error')
            return redirect(url_for('group_daily_attendance', group_id=group.id))

        students = _attendance_roster_query(group.id).order_by(Student.name).all()
        if request.method == 'POST':
            submitted = []
            for student in students:
                status = request.form.get(f'status_{student.id}', '')
                notes = request.form.get(f'notes_{student.id}', '').strip()
                if status not in ATTENDANCE_STATUS_LABELS or len(notes) > 500:
                    flash('Har bir o‘quvchi uchun holatni tanlang va izohni tekshiring.', 'error')
                    return redirect(url_for(
                        'group_daily_attendance', group_id=group.id,
                        date=attendance_date.isoformat(),
                    ))
                submitted.append((student, status, notes))

            records = {
                record.student_id: record
                for record in Attendance.query.filter_by(
                    group_id=group.id, session_date=attendance_date, session_id=None,
                ).all()
            }
            now = datetime.now()
            for student, status, notes in submitted:
                record = records.get(student.id)
                if record is None:
                    record = Attendance(
                        student=student, group=group, session_date=attendance_date,
                        session_id=None, status=status, notes=notes,
                    )
                    db.session.add(record)
                else:
                    record.status = status
                    record.notes = notes
                record.marked_by_id = current_user.id
                record.marked_at = now
            try:
                db.session.commit()
                flash('Guruh davomati saqlandi.', 'success')
            except IntegrityError:
                db.session.rollback()
                flash('Davomat boshqa foydalanuvchi tomonidan yangilandi. Sahifani tekshirib qayta saqlang.', 'error')
            return redirect(url_for(
                'group_daily_attendance', group_id=group.id,
                date=attendance_date.isoformat(),
            ))

        records = {
            record.student_id: record
            for record in Attendance.query.filter_by(
                group_id=group.id, session_date=attendance_date, session_id=None,
            ).all()
        }
        monthly_percentages = calculate_monthly_attendance_percentages(
            group.id, attendance_date.year, attendance_date.month,
        )
        return render_template(
            'daily_attendance.html', group=group, students=students, records=records,
            attendance_date=attendance_date,
            today_iso=date.today().isoformat(),
            monthly_percentages=monthly_percentages,
            status_labels=ATTENDANCE_STATUS_LABELS,
        )

    @app.post('/groups/<int:group_id>/students')
    @roles_required('admin')
    def add_group_student(group_id):
        group = db.get_or_404(Group, group_id)
        try:
            student = db.get_or_404(Student, int(request.form.get('student_id', '')))
        except (ValueError, TypeError):
            flash('O‘quvchi tanlovini tekshiring.', 'error')
            return redirect(url_for('group_detail', group_id=group.id))
        if not group.is_active or student.status != 'active':
            flash('Faqat faol guruhga faol o‘quvchi qo‘shish mumkin.', 'error')
        else:
            if group not in student.groups:
                student.groups.append(group)
            if student.group is None:
                student.group = group
            db.session.commit()
            flash('O‘quvchi guruhga qo‘shildi.', 'success')
        return redirect(url_for('group_detail', group_id=group.id))

    @app.post('/groups/<int:group_id>/students/<int:student_id>/remove')
    @roles_required('admin')
    def remove_group_student(group_id, student_id):
        group = db.get_or_404(Group, group_id)
        student = db.get_or_404(Student, student_id)
        if group in student.groups:
            student.groups.remove(group)
        if student.group_id == group.id:
            student.group = student.groups[0] if student.groups else None
        db.session.commit()
        flash('O‘quvchi guruhdan chiqarildi.', 'success')
        return redirect(url_for('group_detail', group_id=group.id))

    @app.route('/groups/<int:group_id>/edit', methods=['GET', 'POST'])
    @roles_required('admin')
    def edit_group(group_id):
        group = db.get_or_404(Group, group_id)
        teachers = User.query.filter_by(role='teacher', enabled=True).order_by(User.full_name).all()
        courses = Course.query.order_by(Course.title).all()
        if request.method == 'POST':
            try:
                values = _group_form_values(request.form)
                group.title = values['title']
                group.course = values['course']
                group.course_name = values['course'].title
                group.teacher = values['teacher']
                group.schedule = values['schedule']
                group.schedule_days = values['schedule_days']
                group.start_time = values['start_time']
                group.room_number = values['room_number']
                group.is_active = request.form.get('is_active') == 'on'
                db.session.commit()
                flash('Guruh ma’lumotlari saqlandi.', 'success')
                return redirect(url_for('group_detail', group_id=group.id))
            except (ValueError, TypeError):
                db.session.rollback()
                flash('Guruh ma’lumotlarini tekshiring.', 'error')
            except IntegrityError:
                db.session.rollback()
                flash('Bu guruh nomi band.', 'error')
        return render_template('group_form.html', group=group, teachers=teachers, courses=courses)

    @app.post('/groups/<int:group_id>/delete')
    @roles_required('admin')
    def delete_group(group_id):
        group = db.get_or_404(Group, group_id)
        if group.enrolled_students or group.students or group.sessions or group.attendance_records:
            flash('Tarixni saqlash uchun faqat o‘quvchi va darslari yo‘q guruhni o‘chirish mumkin.', 'error')
        else:
            db.session.delete(group)
            db.session.commit()
            flash('Guruh o‘chirildi.', 'success')
        return redirect(url_for('groups'))

    @app.route('/sessions', methods=['GET', 'POST'])
    @roles_required('admin', 'teacher')
    def sessions():
        available_groups = _group_scope_query(current_user).filter(Group.is_active.is_(True)).order_by(Group.name).all()
        if request.method == 'POST':
            try:
                group = db.get_or_404(Group, int(request.form.get('group_id', '')))
                if not _can_manage_group(current_user, group):
                    abort(403)
                starts_at = _parse_datetime_local(request.form.get('starts_at', ''))
                topic = request.form.get('topic', '').strip()
                db.session.add(ClassSession(
                    group=group, starts_at=starts_at, topic=topic, created_by_id=current_user.id,
                ))
                db.session.commit()
                flash('Dars mashg\'uloti yaratildi.', 'success')
                return redirect(url_for('sessions'))
            except (ValueError, KeyError):
                db.session.rollback()
                flash("Mashg'ulot ma'lumotlarini tekshiring.", 'error')
        visible_group_ids = [group.id for group in available_groups]
        upcoming = ClassSession.query.filter(ClassSession.group_id.in_(visible_group_ids)) if visible_group_ids else ClassSession.query.filter(db.false())
        return render_template(
            'sessions.html', groups=available_groups,
            sessions=upcoming.order_by(ClassSession.starts_at.desc()).limit(100).all(),
            today_label=date.today().strftime('%d.%m.%Y'),
        )

    @app.route('/sessions/<int:session_id>/attendance', methods=['GET', 'POST'])
    @roles_required('admin', 'teacher')
    def session_attendance(session_id):
        class_session = db.get_or_404(ClassSession, session_id)
        if not _can_manage_group(current_user, class_session.group):
            abort(403)
        group_students = Student.query.filter(
            Student.status == 'active',
            or_(
                Student.group_id == class_session.group_id,
                Student.groups.any(Group.id == class_session.group_id),
            ),
        ).order_by(Student.name).all()
        if request.method == 'POST':
            submitted = []
            for student in group_students:
                status = request.form.get(f'status_{student.id}')
                if status not in ('present', 'absent', 'late'):
                    flash("Har bir o'quvchi uchun holat tanlang.", 'error')
                    return redirect(url_for('session_attendance', session_id=session_id))
                submitted.append((student, status))
            for student, status in submitted:
                record = Attendance.query.filter_by(student_id=student.id, session_id=session_id).first()
                status_changed = record is None or record.status != status
                if record is None:
                    record = Attendance(
                        student=student, session=class_session, group=class_session.group,
                        session_date=class_session.starts_at.date(), status=status,
                    )
                    db.session.add(record)
                else:
                    record.status = status
                record.marked_by_id = current_user.id
                record.marked_at = datetime.now()
                if status_changed:
                    db.session.flush()
                    _notify_student_parents(
                        student,
                        'attendance_update',
                        'Davomat yangilandi',
                        f"{class_session.group.name}: {student.name} - { {'present': 'keldi', 'absent': 'kelmadi', 'late': 'kechikdi'}[status] }.",
                        f'attendance:{record.id}:{record.marked_at.isoformat()}',
                        url_for('dashboard'),
                    )
            db.session.commit()
            flash('Davomat saqlandi.', 'success')
            return redirect(url_for('session_attendance', session_id=session_id))
        records = {record.student_id: record for record in class_session.attendances}
        return render_template(
            'session_attendance.html', class_session=class_session,
            students=group_students, records=records,
        )

    @app.route('/payments', methods=['GET', 'POST'])
    @login_required
    def payments():
        if request.method == 'POST':
            if current_user.role != 'admin':
                abort(403)
            try:
                student = db.get_or_404(Student, int(request.form.get('student_id', '')))
                amount = Decimal(request.form.get('amount', ''))
                month = request.form.get('month_covered', request.form.get('month', ''))
                due_date = _parse_date(request.form.get('due_date', ''))
                status = request.form.get('status', '')
                payment_type = request.form.get('payment_type', 'cash')
                payment_date = _parse_date(request.form['payment_date']) if request.form.get('payment_date') else date.today()
                receipt_number = request.form.get('receipt_number', '').strip() or None
                datetime.strptime(month, '%Y-%m')
                if (
                    not amount.is_finite() or amount <= 0 or status not in ('paid', 'pending')
                    or payment_type not in ('cash', 'card') or len(receipt_number or '') > 40
                ):
                    raise ValueError
                if receipt_number and Payment.query.filter_by(receipt_number=receipt_number).first():
                    raise ValueError
                payment = Payment(
                    student=student, amount=amount, month=month, due_date=due_date,
                    payment_type=payment_type,
                    payment_date=payment_date if status == 'paid' else None,
                    receipt_number=receipt_number,
                    status=status, paid_at=datetime.now() if status == 'paid' else None,
                )
                db.session.add(payment)
                db.session.flush()
                if status == 'paid' and payment.receipt_number is None:
                    payment.receipt_number = f'PAY-{date.today():%Y%m%d}-{payment.id:06d}'
                _sync_student_payment_state(student)
                db.session.commit()
                flash("To'lov yozuvi saqlandi.", 'success')
                return redirect(url_for('payments'))
            except (ValueError, InvalidOperation, KeyError, TypeError):
                db.session.rollback()
                flash("To'lov ma'lumotlarini tekshiring.", 'error')

        base_query = Payment.query.join(Student)
        if current_user.role == 'teacher':
            base_query = base_query.filter(Student.groups.any(Group.teacher_id == current_user.id))
        elif current_user.role == 'student_parent':
            base_query = base_query.join(Student.parents).filter(User.id == current_user.id)
        start_date = request.args.get('from_date', '').strip()
        end_date = request.args.get('to_date', '').strip()
        student_filter = request.args.get('student_id', '').strip()
        try:
            start_day = _parse_date(start_date) if start_date else None
            end_day = _parse_date(end_date) if end_date else None
            if start_day and end_day and start_day > end_day:
                raise ValueError
            if start_day:
                base_query = base_query.filter(Payment.payment_date >= start_day)
            if end_day:
                base_query = base_query.filter(Payment.payment_date <= end_day)
            if student_filter:
                base_query = base_query.filter(Payment.student_id == int(student_filter))
        except (ValueError, TypeError):
            flash("Sana yoki o'quvchi filtri noto'g'ri.", 'error')
            return redirect(url_for('payments'))
        revenue = base_query.filter(Payment.status == 'paid').with_entities(
            func.coalesce(func.sum(Payment.amount), 0)
        ).scalar()
        query = base_query
        status_filter = request.args.get('status', '').strip()
        if status_filter == 'overdue':
            query = query.filter(Payment.status == 'pending', Payment.due_date < date.today())
        elif status_filter in ('paid', 'pending'):
            query = query.filter(Payment.status == status_filter)
        payment_page = db.paginate(
            query.order_by(Payment.payment_date.desc(), Payment.month.desc(), Payment.id.desc()).statement,
            per_page=50, max_per_page=100, error_out=False,
        )
        students_for_payment = Student.query.order_by(Student.name).all() if current_user.role == 'admin' else []
        filter_students = _student_scope_query(current_user).order_by(Student.name).all()
        return render_template(
            'payments.html', payments=payment_page.items, payment_page=payment_page,
            students=students_for_payment, filter_students=filter_students,
            status_filter=status_filter, revenue=revenue,
            from_date=start_date, to_date=end_date, student_filter=student_filter,
            today_iso=date.today().isoformat(),
        )

    @app.get('/payments/debtors')
    @roles_required('admin')
    def payment_debtors():
        overdue_students = Student.query.join(Payment).filter(
            Payment.status == 'pending', Payment.due_date < date.today(),
        ).distinct().order_by(Student.name).all()
        for student in overdue_students:
            _sync_student_payment_state(student)
        if overdue_students:
            db.session.commit()
        return render_template('debtors.html', students=overdue_students)

    @app.post('/payments/<int:payment_id>/mark-paid')
    @roles_required('admin')
    def mark_payment_paid(payment_id):
        payment = db.get_or_404(Payment, payment_id)
        payment.status = 'paid'
        payment.paid_at = datetime.now()
        payment.payment_date = date.today()
        if payment.receipt_number is None:
            payment.receipt_number = f'PAY-{date.today():%Y%m%d}-{payment.id:06d}'
        _sync_student_payment_state(payment.student)
        db.session.commit()
        flash("To'lov amalga oshirilgan deb belgilandi.", 'success')
        return redirect(url_for('payments'))

    @app.get('/users', endpoint='users')
    @app.get('/admin/users', endpoint='admin_users')
    @admin_required
    def users():
        return render_template('users.html', users=User.query.order_by(User.full_name).all())

    @app.post('/admin/users/create')
    @admin_required
    def create_user():
        name = request.form.get('full_name', '').strip()
        raw_email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', '')
        password = request.form.get('password', '')
        confirmation = request.form.get('password_confirmation', '')
        try:
            email = validate_email(raw_email, check_deliverability=False).normalized
        except EmailNotValidError:
            email = ''

        if (
            len(name) < 2 or len(name) > 120 or not email or len(email) > 255
            or role not in ('admin', 'teacher', 'student_parent')
            or len(password) < 8 or password != confirmation
        ):
            flash("Hisob ma'lumotlarini tekshiring. Parol kamida 8 belgi bo'lsin.", 'error')
            return redirect(url_for('admin_users'))
        if User.query.filter(func.lower(User.email) == email).first():
            flash("Bu email allaqachon ro'yxatdan o'tgan.", 'error')
            return redirect(url_for('admin_users'))

        user = User(full_name=name, email=email, role=role)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Bu email allaqachon ro'yxatdan o'tgan.", 'error')
            return redirect(url_for('admin_users'))
        flash('User successfully created!', 'success')
        return redirect(url_for('admin_users'))

    @app.post('/users/<int:user_id>/toggle')
    @admin_required
    def toggle_user(user_id):
        user = db.get_or_404(User, user_id)
        if user.id == current_user.id:
            flash("Joriy administrator hisobini o'chirib bo'lmaydi.", 'error')
        elif user.role == 'admin' and user.enabled and User.query.filter_by(role='admin', enabled=True).count() <= 1:
            flash("Oxirgi faol administrator hisobini o'chirib bo'lmaydi.", 'error')
        else:
            user.enabled = not user.enabled
            db.session.commit()
            flash('Hisob holati yangilandi.', 'success')
        return redirect(url_for('admin_users'))


def register_cli(app):
    @app.cli.command('init-db')
    def init_db_command():
        """Create new tables and safely upgrade the legacy SQLite database."""
        initialize_database(app)
        click.echo('Database initialized; legacy records were preserved.')

    @app.cli.command('create-admin')
    @click.option('--email', prompt=True)
    @click.option('--name', prompt='Full name')
    def create_admin_command(email, name):
        """Create an administrator interactively with a hidden password prompt."""
        try:
            email = validate_email(email.strip().lower(), check_deliverability=False).normalized
        except EmailNotValidError as error:
            raise click.ClickException(str(error)) from error
        if User.query.filter(func.lower(User.email) == email).first():
            raise click.ClickException('An account with that email already exists.')
        password = click.prompt('Password (minimum 12 characters)', hide_input=True, confirmation_prompt=True)
        if len(password) < 12:
            raise click.ClickException('Password must contain at least 12 characters.')
        user = User(full_name=name.strip(), email=email, role='admin')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f'Administrator created: {email}')

    @app.cli.command('send-reminders')
    @click.option('--days', type=click.IntRange(1, 30), default=3, show_default=True)
    def send_reminders_command(days):
        """Queue in-app tuition and upcoming-class reminders; schedule this daily."""
        now = datetime.now()
        today = date.today()
        due_through = today + timedelta(days=days)
        created = 0

        due_payments = Payment.query.filter(
            Payment.status == 'pending',
            Payment.due_date.is_not(None),
            Payment.due_date <= due_through,
        ).all()
        for payment in due_payments:
            due_label = payment.due_date.strftime('%d.%m.%Y')
            for parent in payment.student.parents:
                if parent.enabled and parent.role == 'student_parent':
                    created += _create_notification(
                        parent,
                        'payment_due',
                        "To'lov muddati yaqin",
                        f"{payment.student.name} uchun {payment.month} oyi to'lovi {due_label} sanagacha kutilmoqda.",
                        f'payment:{payment.id}:due:{payment.due_date.isoformat()}',
                        '/payments',
                    )

        upcoming_sessions = ClassSession.query.join(Group).filter(
            Group.is_active.is_(True),
            ClassSession.starts_at >= now,
            ClassSession.starts_at <= now + timedelta(days=days),
        ).all()
        for class_session in upcoming_sessions:
            start_label = class_session.starts_at.strftime('%d.%m.%Y %H:%M')
            group_students = Student.query.filter(or_(
                Student.group_id == class_session.group_id,
                Student.groups.any(Group.id == class_session.group_id),
            )).all()
            for student in group_students:
                if student.status != 'active':
                    continue
                for parent in student.parents:
                    if parent.enabled and parent.role == 'student_parent':
                        created += _create_notification(
                            parent,
                            'class_reminder',
                            'Yaqinlashayotgan dars',
                            f"{class_session.group.name} darsi {start_label} da boshlanadi. {class_session.topic}".strip(),
                            f'class:{class_session.id}:reminder',
                            '/',
                        )

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise click.ClickException('Reminder batch conflicted with another run; run it again safely.')
        click.echo(f'Queued {created} in-app reminder(s).')


def register_error_handlers(app):
    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        flash('Xavfsizlik tokeni yaroqsiz yoki muddati tugagan. Qayta urinib ko\'ring.', 'error')
        destination = url_for('dashboard') if current_user.is_authenticated else url_for('login')
        return redirect(destination)

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        if error.code == 403:
            return render_template('errors/403.html'), 403
        if error.code == 404:
            return render_template('errors/404.html'), 404
        return render_template('errors/generic.html', error=error), error.code


app = create_app()


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')