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
from models import Attendance, ClassSession, Group, Inquiry, Notification, Payment, Student, User
from schema_bootstrap import initialize_database


ROLE_LABELS = {
    'admin': 'Administrator',
    'teacher': "O'qituvchi",
    'student_parent': "O'quvchi / ota-ona",
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
        query = query.join(Group).filter(Group.teacher_id == user.id)
    elif user.role == 'student_parent':
        query = query.join(Student.parents).filter(User.id == user.id)
    return query


def _group_scope_query(user):
    query = Group.query
    if user.role == 'teacher':
        query = query.filter(Group.teacher_id == user.id)
    elif user.role == 'student_parent':
        query = query.join(Group.students).join(Student.parents).filter(User.id == user.id)
    return query.distinct()


def _can_manage_group(user, group):
    return user.role == 'admin' or (user.role == 'teacher' and group.teacher_id == user.id)


def _parse_date(value):
    return datetime.strptime(value, '%Y-%m-%d').date()


def _parse_datetime_local(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M')


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

    @app.route('/register', methods=['GET', 'POST'])
    @limiter.limit('5 per hour', methods=['POST'])
    def register():
        if request.method == 'POST':
            name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            confirmation = request.form.get('password_confirmation', '')
            try:
                email = validate_email(email, check_deliverability=False).normalized
                email_is_valid = True
            except EmailNotValidError:
                email_is_valid = False
            if len(name) < 2 or len(name) > 120:
                flash("Ism kamida 2 ta belgidan iborat bo'lishi kerak.", 'error')
            elif not email_is_valid or len(email) > 255:
                flash("Email manzilini tekshiring.", 'error')
            elif len(password) < 12:
                flash("Parol kamida 12 ta belgidan iborat bo'lishi kerak.", 'error')
            elif password != confirmation:
                flash("Parollar mos kelmadi.", 'error')
            elif User.query.filter(func.lower(User.email) == email).first():
                flash("Bu email allaqachon ro'yxatdan o'tgan.", 'error')
            else:
                user = User(full_name=name, email=email, role='student_parent')
                user.set_password(password)
                db.session.add(user)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("Bu email allaqachon ro'yxatdan o'tgan.", 'error')
                    return render_template('auth/register.html')
                login_user(user)
                flash("Hisob yaratildi. Farzandingizni administrator bog'lab beradi.", 'success')
                return redirect(url_for('dashboard'))
        return render_template('auth/register.html')

    @app.post('/logout')
    @login_required
    def logout():
        logout_user()
        flash('Tizimdan chiqdingiz.', 'success')
        return redirect(url_for('login'))

    @app.get('/')
    @login_required
    def dashboard():
        today = date.today()
        this_month = today.strftime('%Y-%m')
        student_query = _student_scope_query(current_user)
        group_query = _group_scope_query(current_user)
        payment_query = Payment.query.join(Student)
        if current_user.role == 'teacher':
            payment_query = payment_query.join(Group).filter(Group.teacher_id == current_user.id)
        elif current_user.role == 'student_parent':
            payment_query = payment_query.join(Student.parents).filter(User.id == current_user.id)

        income = payment_query.filter(Payment.status == 'paid', Payment.month == this_month).with_entities(
            func.coalesce(func.sum(Payment.amount), 0)
        ).scalar()
        pending_query = payment_query.filter(Payment.status == 'pending')
        paid_count = payment_query.filter(Payment.status == 'paid').count()
        pending_count = pending_query.filter(or_(Payment.due_date.is_(None), Payment.due_date >= today)).count()
        overdue_count = pending_query.filter(Payment.due_date < today).count()
        debt_total = pending_query.with_entities(func.coalesce(func.sum(Payment.amount), 0)).scalar()
        notifications = Notification.query.filter_by(user_id=current_user.id).order_by(
            Notification.created_at.desc(), Notification.id.desc(),
        ).limit(6).all()
        upcoming_sessions = ClassSession.query.join(Group).filter(
            ClassSession.starts_at >= datetime.now()
        )
        if current_user.role == 'teacher':
            upcoming_sessions = upcoming_sessions.filter(Group.teacher_id == current_user.id)
        elif current_user.role == 'student_parent':
            upcoming_sessions = upcoming_sessions.join(Group.students).join(Student.parents).filter(User.id == current_user.id)

        return render_template(
            'dashboard.html',
            student_count=student_query.distinct().count(),
            group_count=group_query.filter(Group.is_active.is_(True)).count(),
            monthly_income=income,
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
                    status=status, group=group, parents=linked_parents,
                )
                db.session.add(student)
                db.session.commit()
                flash("O'quvchi qo'shildi.", 'success')
                return redirect(url_for('students'))
            except (ValueError, KeyError):
                db.session.rollback()
                flash("O'quvchi ma'lumotlarini tekshiring.", 'error')
        query = Student.query.join(Group, isouter=True)
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

    @app.route('/groups', methods=['GET', 'POST'])
    @roles_required('admin')
    def groups():
        teachers = User.query.filter_by(role='teacher', enabled=True).order_by(User.full_name).all()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            course_name = request.form.get('course_name', '').strip()
            schedule = request.form.get('schedule', '').strip()
            teacher_id = request.form.get('teacher_id') or None
            teacher = db.session.get(User, int(teacher_id)) if teacher_id else None
            if (
                not name or len(name) > 100 or not course_name or len(course_name) > 100
                or len(schedule) > 250
                or (teacher_id and (teacher is None or teacher.role != 'teacher' or not teacher.enabled))
            ):
                flash("Guruh ma'lumotlarini tekshiring.", 'error')
            else:
                db.session.add(Group(name=name, course_name=course_name, schedule=schedule, teacher=teacher))
                try:
                    db.session.commit()
                    flash("Guruh yaratildi.", 'success')
                    return redirect(url_for('groups'))
                except IntegrityError:
                    db.session.rollback()
                    flash("Bu guruh nomi band.", 'error')
        return render_template('groups.html', groups=Group.query.order_by(Group.name).all(), teachers=teachers)

    @app.route('/groups/<int:group_id>/edit', methods=['GET', 'POST'])
    @roles_required('admin')
    def edit_group(group_id):
        group = db.get_or_404(Group, group_id)
        teachers = User.query.filter_by(role='teacher', enabled=True).order_by(User.full_name).all()
        if request.method == 'POST':
            teacher_id = request.form.get('teacher_id') or None
            teacher = db.session.get(User, int(teacher_id)) if teacher_id else None
            if teacher_id and (teacher is None or teacher.role != 'teacher' or not teacher.enabled):
                flash("O'qituvchi tanlovini tekshiring.", 'error')
            else:
                group.name = request.form.get('name', '').strip()
                group.course_name = request.form.get('course_name', '').strip()
                group.schedule = request.form.get('schedule', '').strip()
                group.teacher = teacher
                group.is_active = request.form.get('is_active') == 'on'
                if group.name and len(group.name) <= 100 and group.course_name and len(group.course_name) <= 100 and len(group.schedule) <= 250:
                    try:
                        db.session.commit()
                        flash("Guruh ma'lumotlari saqlandi.", 'success')
                        return redirect(url_for('groups'))
                    except IntegrityError:
                        db.session.rollback()
                        flash("Bu guruh nomi band.", 'error')
                else:
                    flash("Guruh nomi va kurs majburiy.", 'error')
        return render_template('group_form.html', group=group, teachers=teachers)

    @app.post('/groups/<int:group_id>/delete')
    @roles_required('admin')
    def delete_group(group_id):
        group = db.get_or_404(Group, group_id)
        if group.students or group.sessions:
            flash("Tarixni saqlash uchun faqat o'quvchi va darslari yo'q guruhni o'chirish mumkin.", 'error')
        else:
            db.session.delete(group)
            db.session.commit()
            flash("Guruh o'chirildi.", 'success')
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
        )

    @app.route('/sessions/<int:session_id>/attendance', methods=['GET', 'POST'])
    @roles_required('admin', 'teacher')
    def session_attendance(session_id):
        class_session = db.get_or_404(ClassSession, session_id)
        if not _can_manage_group(current_user, class_session.group):
            abort(403)
        group_students = Student.query.filter_by(
            group_id=class_session.group_id, status='active'
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
                    record = Attendance(student=student, session=class_session, status=status)
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
                month = request.form.get('month', '')
                due_date = _parse_date(request.form.get('due_date', ''))
                status = request.form.get('status', '')
                datetime.strptime(month, '%Y-%m')
                if not amount.is_finite() or amount <= 0 or status not in ('paid', 'pending'):
                    raise ValueError
                payment = Payment(
                    student=student, amount=amount, month=month, due_date=due_date,
                    status=status, paid_at=datetime.now() if status == 'paid' else None,
                )
                db.session.add(payment)
                db.session.commit()
                flash("To'lov yozuvi saqlandi.", 'success')
                return redirect(url_for('payments'))
            except (ValueError, InvalidOperation, KeyError):
                db.session.rollback()
                flash("To'lov ma'lumotlarini tekshiring.", 'error')

        query = Payment.query.join(Student)
        if current_user.role == 'teacher':
            query = query.join(Group).filter(Group.teacher_id == current_user.id)
        elif current_user.role == 'student_parent':
            query = query.join(Student.parents).filter(User.id == current_user.id)
        status_filter = request.args.get('status', '').strip()
        if status_filter == 'overdue':
            query = query.filter(Payment.status == 'pending', Payment.due_date < date.today())
        elif status_filter in ('paid', 'pending'):
            query = query.filter(Payment.status == status_filter)
        payment_page = db.paginate(
            query.order_by(Payment.month.desc(), Payment.id.desc()).statement,
            per_page=50, max_per_page=100, error_out=False,
        )
        students_for_payment = Student.query.order_by(Student.name).all() if current_user.role == 'admin' else []
        return render_template(
            'payments.html', payments=payment_page.items, payment_page=payment_page,
            students=students_for_payment, status_filter=status_filter,
        )

    @app.post('/payments/<int:payment_id>/mark-paid')
    @roles_required('admin')
    def mark_payment_paid(payment_id):
        payment = db.get_or_404(Payment, payment_id)
        payment.status = 'paid'
        payment.paid_at = datetime.now()
        db.session.commit()
        flash("To'lov amalga oshirilgan deb belgilandi.", 'success')
        return redirect(url_for('payments'))

    @app.route('/users', methods=['GET', 'POST'])
    @roles_required('admin')
    def users():
        if request.method == 'POST':
            name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            role = request.form.get('role', '')
            password = request.form.get('password', '')
            try:
                email = validate_email(email, check_deliverability=False).normalized
                email_is_valid = True
            except EmailNotValidError:
                email_is_valid = False
            if (
                not name or len(name) > 120 or not email_is_valid or len(email) > 255
                or role not in ('admin', 'teacher', 'student_parent') or len(password) < 12
            ):
                flash("Hisob ma'lumotlarini tekshiring (parol kamida 12 belgi).", 'error')
            elif User.query.filter(func.lower(User.email) == email).first():
                flash("Bu email allaqachon ro'yxatdan o'tgan.", 'error')
            else:
                user = User(full_name=name, email=email, role=role)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash('Hisob yaratildi.', 'success')
                return redirect(url_for('users'))
        return render_template('users.html', users=User.query.order_by(User.full_name).all())

    @app.post('/users/<int:user_id>/toggle')
    @roles_required('admin')
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
        return redirect(url_for('users'))


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
            for student in class_session.group.students:
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