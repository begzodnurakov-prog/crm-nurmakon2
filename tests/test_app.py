import unittest
from datetime import date, datetime, timedelta

from app import calculate_monthly_attendance_percentages, create_app
from extensions import db
from models import Attendance, ClassSession, Course, Group, Inquiry, Notification, Payment, Student, User


class TestConfig:
    TESTING = True
    SECRET_KEY = 'test-only-secret'
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class CRMWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.admin = self._user('Admin', 'admin@example.test', 'admin')
            self.teacher = self._user('Teacher', 'teacher@example.test', 'teacher')
            self.parent = self._user('Parent', 'parent@example.test', 'student_parent')
            self.other_parent = self._user('Other Parent', 'other-parent@example.test', 'student_parent')
            self.group = Group(
                name='Algebra A', course_name='Algebra', teacher=self.teacher,
                schedule='Mon/Wed 18:00',
            )
            self.student = Student(
                name='Test Student', phone='+998901234567', course='Algebra',
                enrollment_date=date.today(), group=self.group, parents=[self.parent],
            )
            self.other_group = Group(name='Unassigned group', course_name='Science')
            self.other_student = Student(
                name='Other Student', phone='+998909876543', course='Science',
                enrollment_date=date.today(), group=self.other_group,
            )
            db.session.add_all([self.group, self.other_group, self.student, self.other_student])
            db.session.commit()
            self.admin_id = self.admin.id
            self.teacher_id = self.teacher.id
            self.parent_id = self.parent.id
            self.other_parent_id = self.other_parent.id
            self.group_id = self.group.id
            self.other_group_id = self.other_group.id
            self.student_id = self.student.id
            self.other_student_id = self.other_student.id

    @staticmethod
    def _user(name, email, role):
        user = User(full_name=name, email=email, role=role)
        user.set_password('Correct-Horse-42!')
        db.session.add(user)
        db.session.flush()
        return user

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _login(self, email):
        return self.client.post('/login', data={
            'email': email,
            'password': 'Correct-Horse-42!',
        }, follow_redirects=True)

    def test_auth_and_role_scoped_pages(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self._login('admin@example.test').status_code, 200)
        self.assertEqual(self.client.get('/students').status_code, 200)
        self.client.post('/logout')

        self._login('teacher@example.test')
        self.assertEqual(self.client.get('/sessions').status_code, 200)
        self.assertEqual(self.client.get('/students').status_code, 403)
        self.client.post('/logout')

        self._login('parent@example.test')
        response = self.client.get('/')
        self.assertIn(b'Test Student', response.data)
        self.assertNotIn(b'Other Student', response.data)
        self.assertEqual(self.client.get('/users').status_code, 403)

    def test_dashboard_kpis_and_chart_data_use_live_metrics(self):
        today = date.today()
        previous_month = date(today.year, today.month, 1) - timedelta(days=1)
        previous_payment_date = previous_month.replace(day=15)
        with self.app.app_context():
            other_student = db.session.get(Student, self.other_student_id)
            other_student.status = 'completed'
            db.session.add(Student(
                name='Chemistry Learner', phone='+998901010101', course='Chemistry',
                enrollment_date=today, group=self.group,
            ))
            db.session.add_all([
                Payment(
                    student_id=self.student_id, amount=600000,
                    month=today.strftime('%Y-%m'), status='paid', payment_date=today,
                ),
                Payment(
                    student_id=self.student_id, amount=300000,
                    month=previous_payment_date.strftime('%Y-%m'),
                    status='paid', payment_date=previous_payment_date,
                ),
                Payment(
                    student_id=self.student_id, amount=75000,
                    month=today.strftime('%Y-%m'), status='pending',
                    due_date=today - timedelta(days=1),
                ),
            ])
            db.session.commit()

        self._login('admin@example.test')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Faol o\'quvchilar', response.data)
        self.assertIn(b'Qarzdorlar', response.data)
        self.assertIn(b'600,000', response.data)
        self.assertIn(b'Oylik daromad tendensiyasi', response.data)
        self.assertIn(b'Kurslar taqsimoti', response.data)
        self.assertIn(b'id="revenueTrendChart"', response.data)
        self.assertIn(b'id="courseDistributionChart"', response.data)
        self.assertIn(b'300000.0', response.data)
        self.assertIn(b'600000.0', response.data)
        self.assertIn(b'Algebra', response.data)
        self.assertIn(b'Chemistry', response.data)

    def test_parent_registration_never_grants_privileged_role(self):
        response = self.client.post('/register', data={
            'full_name': 'New Parent',
            'email': 'new-parent@example.com',
            'password': 'Long-Password-903!',
            'password_confirmation': 'Long-Password-903!',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = User.query.filter_by(email='new-parent@example.com').one()
            self.assertEqual(user.role, 'student_parent')
            self.assertTrue(user.check_password('Long-Password-903!'))

    def test_teacher_creates_session_and_marks_late_attendance(self):
        self._login('teacher@example.test')
        response = self.client.post('/sessions', data={
            'group_id': str(self.group_id),
            'starts_at': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
            'topic': 'Linear equations',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            class_session = ClassSession.query.filter_by(group_id=self.group_id).one()
            session_id = class_session.id
        response = self.client.post(
            f'/sessions/{session_id}/attendance',
            data={f'status_{self.student_id}': 'late'},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            record = Attendance.query.filter_by(
                student_id=self.student_id, session_id=session_id,
            ).one()
            self.assertEqual(record.status, 'late')
            parent_id = self.parent_id
            first_notification_count = Notification.query.filter_by(user_id=parent_id).count()
            self.assertEqual(first_notification_count, 1)
        self.client.post(
            f'/sessions/{session_id}/attendance',
            data={f'status_{self.student_id}': 'late'},
        )
        with self.app.app_context():
            self.assertEqual(Notification.query.filter_by(user_id=parent_id).count(), 1)
        self.client.post(
            f'/sessions/{session_id}/attendance',
            data={f'status_{self.student_id}': 'absent'},
        )
        with self.app.app_context():
            self.assertEqual(Notification.query.filter_by(user_id=parent_id).count(), 2)

    def test_daily_group_attendance_batch_and_monthly_percentages(self):
        today = date.today()
        with self.app.app_context():
            second_student = db.session.get(Student, self.other_student_id)
            second_student.groups.append(self.group)
            excused_student = Student(
                name='Excused Student', phone='+998901111111', course='Algebra',
                enrollment_date=today, group=self.group, groups=[self.group],
            )
            db.session.add(excused_student)
            db.session.flush()
            excused_student_id = excused_student.id
            db.session.add(Attendance(
                student_id=excused_student_id, group_id=self.group_id,
                session_date=today - timedelta(days=1), status='present', session_id=None,
            ))
            history_date = today - timedelta(days=2)
            history_session = ClassSession(
                group=self.group, starts_at=datetime.combine(history_date, datetime.min.time()),
                topic='Monthly analytics history',
            )
            db.session.add(history_session)
            db.session.flush()
            db.session.add(Attendance(
                student_id=excused_student_id, group_id=self.group_id,
                session=history_session, session_date=history_date, status='absent',
            ))
            db.session.commit()

        self._login('teacher@example.test')
        page = self.client.get(f'/groups/{self.group_id}/attendance')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Barchasini kelgan deb belgilash', page.data)
        invalid = self.client.post(f'/groups/{self.group_id}/attendance', data={
            'status_{}'.format(self.student_id): 'present',
        })
        self.assertEqual(invalid.status_code, 302)
        with self.app.app_context():
            self.assertEqual(Attendance.query.filter_by(
                group_id=self.group_id, session_date=today, session_id=None,
            ).count(), 0)

        response = self.client.post(f'/groups/{self.group_id}/attendance', data={
            f'status_{self.student_id}': 'present',
            f'notes_{self.student_id}': 'Ready on time',
            f'status_{self.other_student_id}': 'absent',
            f'notes_{self.other_student_id}': 'Parent notified',
            f'status_{excused_student_id}': 'excused',
            f'notes_{excused_student_id}': 'Medical appointment',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            present = Attendance.query.filter_by(
                student_id=self.student_id, group_id=self.group_id,
                session_date=today, session_id=None,
            ).one()
            self.assertEqual(present.status, 'present')
            self.assertEqual(present.notes, 'Ready on time')
            rates = calculate_monthly_attendance_percentages(
                self.group_id, today.year, today.month,
            )
            self.assertEqual(rates[self.student_id], 100.0)
            self.assertEqual(rates[self.other_student_id], 0.0)
            self.assertEqual(rates[excused_student_id], 50.0)
        self.assertIn(b'100.0%', self.client.get(
            f'/groups/{self.group_id}/attendance?date={today.isoformat()}'
        ).data)
        self.assertEqual(self.client.get(
            f'/groups/{self.other_group_id}/attendance'
        ).status_code, 403)

    def test_reminder_command_is_idempotent(self):
        with self.app.app_context():
            payment = Payment(
                student_id=self.student_id,
                amount=250000,
                month=date.today().strftime('%Y-%m'),
                status='pending',
                due_date=date.today() + timedelta(days=1),
            )
            class_session = ClassSession(
                group_id=self.group_id,
                starts_at=datetime.now() + timedelta(hours=3),
                topic='Reminder test',
            )
            db.session.add_all([payment, class_session])
            db.session.commit()

        runner = self.app.test_cli_runner()
        first_run = runner.invoke(args=['send-reminders', '--days', '3'])
        self.assertEqual(first_run.exit_code, 0, first_run.output)
        self.assertIn('Queued 2 in-app reminder(s).', first_run.output)
        second_run = runner.invoke(args=['send-reminders', '--days', '3'])
        self.assertEqual(second_run.exit_code, 0, second_run.output)
        self.assertIn('Queued 0 in-app reminder(s).', second_run.output)
        with self.app.app_context():
            notifications = Notification.query.filter_by(user_id=self.parent_id).all()
            self.assertEqual({item.kind for item in notifications}, {'payment_due', 'class_reminder'})

    def test_parent_contact_admin_response_and_notification_ownership(self):
        self._login('parent@example.test')
        response = self.client.post('/contact', data={
            'category': 'payment',
            'student_id': str(self.student_id),
            'subject': 'Payment question',
            'message': 'Could you please explain this month payment?',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            inquiry = Inquiry.query.filter_by(sender_id=self.parent_id).one()
            inquiry_id = inquiry.id

        self.client.post('/logout')
        self._login('admin@example.test')
        self.assertEqual(self.client.get('/admin/inquiries').status_code, 200)
        response = self.client.post(f'/admin/inquiries/{inquiry_id}/update', data={
            'status': 'in_progress',
            'admin_response': 'We checked your account and sent the invoice details.',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            notification = Notification.query.filter_by(
                user_id=self.parent_id, kind='support_reply',
            ).one()
            notification_id = notification.id

        self.client.post('/logout')
        self._login('other-parent@example.test')
        self.assertEqual(self.client.post(f'/notifications/{notification_id}/read').status_code, 404)
        self.client.post('/logout')
        self._login('parent@example.test')
        response = self.client.get('/contact')
        self.assertIn(b'sent the invoice details', response.data)
        response = self.client.post(f'/notifications/{notification_id}/read')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Notification, notification_id).read_at)

    def test_payment_aging_and_settlement(self):
        self._login('admin@example.test')
        due_date = (date.today() - timedelta(days=1)).isoformat()
        response = self.client.post('/payments', data={
            'student_id': str(self.student_id),
            'amount': '325000',
            'month': date.today().strftime('%Y-%m'),
            'due_date': due_date,
            'status': 'pending',
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get('/payments?status=overdue')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Muddati'.encode(), response.data)
        with self.app.app_context():
            payment = Payment.query.filter_by(student_id=self.student_id).one()
            payment_id = payment.id
            student = db.session.get(Student, self.student_id)
            self.assertEqual(student.balance, 325000)
            self.assertEqual(student.payment_status, 'overdue')
        debtors = self.client.get('/payments/debtors')
        self.assertEqual(debtors.status_code, 200)
        self.assertIn(b'Test Student', debtors.data)
        response = self.client.post(f'/payments/{payment_id}/mark-paid')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            payment = db.session.get(Payment, payment_id)
            self.assertEqual(payment.status, 'paid')
            self.assertIsNotNone(payment.paid_at)
            self.assertEqual(payment.payment_type, 'cash')
            self.assertIsNotNone(payment.payment_date)
            self.assertEqual(payment.receipt_number, f'PAY-{date.today():%Y%m%d}-{payment.id:06d}')
            student = db.session.get(Student, self.student_id)
            self.assertEqual(student.balance, 0)
            self.assertEqual(student.payment_status, 'paid')
        dashboard = self.client.get('/')
        self.assertIn(b'data-chart-values="[1, 0, 0]"', dashboard.data)

    def test_payment_collection_metadata_filters_and_revenue(self):
        self._login('admin@example.test')
        response = self.client.post('/payments', data={
            'student_id': str(self.student_id),
            'amount': '225000',
            'month_covered': date.today().strftime('%Y-%m'),
            'due_date': date.today().isoformat(),
            'payment_type': 'card',
            'payment_date': date.today().isoformat(),
            'receipt_number': 'RCPT-TEST-1',
            'status': 'paid',
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get(
            f'/payments?from_date={date.today().isoformat()}&to_date={date.today().isoformat()}&student_id={self.student_id}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'225,000', response.data)
        self.assertIn(b'RCPT-TEST-1', response.data)
        self.assertIn(b'Karta', response.data)
        with self.app.app_context():
            payment = Payment.query.filter_by(receipt_number='RCPT-TEST-1').one()
            self.assertEqual(payment.month_covered, date.today().strftime('%Y-%m'))
            self.assertEqual(payment.payment_type, 'card')
            self.assertEqual(payment.student.balance, 0)

    def test_admin_group_student_crud_archives_without_erasing_history(self):
        self._login('admin@example.test')
        response = self.client.post('/groups', data={
            'name': 'Evening Physics',
            'course_name': 'Physics',
            'teacher_id': str(self.teacher_id),
            'schedule': 'Tue/Thu 19:00',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            group = Group.query.filter_by(name='Evening Physics').one()
            group_id = group.id
        response = self.client.post('/students', data={
            'name': 'New Learner',
            'phone': '+998901112233',
            'course': 'Physics',
            'group_id': str(group_id),
            'enrollment_date': date.today().isoformat(),
            'status': 'active',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            learner = Student.query.filter_by(name='New Learner').one()
            learner_id = learner.id
            db.session.add(Payment(
                student=learner, amount=100000, month=date.today().strftime('%Y-%m'),
                status='pending', due_date=date.today(),
            ))
            db.session.commit()
        response = self.client.post(
            f'/students/{learner_id}/delete',
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            learner = db.session.get(Student, learner_id)
            self.assertEqual(learner.status, 'completed')
            self.assertEqual(Payment.query.filter_by(student_id=learner_id).count(), 1)

    def test_course_group_crud_and_multi_group_enrollment(self):
        self._login('admin@example.test')
        self.assertEqual(self.client.get('/courses').status_code, 200)
        self.assertEqual(self.client.get('/groups').status_code, 200)
        response = self.client.post('/courses', data={
            'title': 'Physics', 'price': '325000', 'duration_months': '8',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            course = Course.query.filter_by(title='Physics').one()
            course_id = course.id
            self.assertEqual(str(course.price), '325000.00')
            self.assertEqual(course.duration_months, 8)
        response = self.client.post(f'/courses/{course_id}/edit', data={
            'title': 'Physics Plus', 'price': '350000', 'duration_months': '9',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(f'/courses/{course_id}/edit').status_code, 200)
        response = self.client.post('/groups', data={
            'title': 'Physics Evening', 'course_id': str(course_id),
            'teacher_id': str(self.teacher_id), 'room_number': '204',
            'schedule_days': 'Tue-Thu-Sat', 'start_time': '18:30',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            group = Group.query.filter_by(title='Physics Evening').one()
            group_id = group.id
            self.assertEqual(group.course.title, 'Physics Plus')
            self.assertEqual(group.course_name, 'Physics Plus')
            self.assertEqual(group.schedule_days, 'Tue-Thu-Sat')
            self.assertEqual(group.start_time.strftime('%H:%M'), '18:30')
            self.assertEqual(group.room_number, '204')
        self.assertEqual(self.client.get(f'/groups/{group_id}/edit').status_code, 200)
        response = self.client.post(f'/groups/{group_id}/edit', data={
            'title': 'Physics Evening Updated', 'course_id': str(course_id),
            'teacher_id': str(self.teacher_id), 'room_number': '205',
            'schedule_days': 'Mon-Wed-Fri', 'start_time': '19:00', 'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            group = db.session.get(Group, group_id)
            self.assertEqual(group.title, 'Physics Evening Updated')
            self.assertEqual(group.room_number, '205')
        self.assertEqual(self.client.get(f'/groups/{group_id}').status_code, 200)
        response = self.client.post(f'/groups/{group_id}/students', data={
            'student_id': str(self.student_id),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            learner = db.session.get(Student, self.student_id)
            self.assertIn(group_id, {group.id for group in learner.groups})
            self.assertEqual(learner.group_id, self.group_id)
        response = self.client.post(f'/students/{self.student_id}/edit', data={
            'name': 'Test Student', 'phone': '+998901234567', 'course': 'Algebra',
            'group_id': str(self.group_id), 'enrollment_date': date.today().isoformat(),
            'status': 'active',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            learner = db.session.get(Student, self.student_id)
            self.assertIn(group_id, {group.id for group in learner.groups})
        detail = self.client.get(f'/groups/{group_id}')
        self.assertIn(b'Test Student', detail.data)
        response = self.client.post(
            f'/groups/{group_id}/students/{self.student_id}/remove',
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            learner = db.session.get(Student, self.student_id)
            self.assertNotIn(group_id, {group.id for group in learner.groups})
            self.assertEqual(learner.group_id, self.group_id)
        response = self.client.post(f'/groups/{group_id}/delete')
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f'/courses/{course_id}/delete')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Group, group_id))
            self.assertIsNone(db.session.get(Course, course_id))

    def test_student_and_payment_pagination_preserve_filters(self):
        with self.app.app_context():
            db.session.add_all([
                Student(
                    name=f'Page Learner {index:02}',
                    phone=f'+99890000{index:04}',
                    course='Algebra', enrollment_date=date.today(), group_id=self.group_id,
                )
                for index in range(30)
            ])
            db.session.add_all([
                Payment(
                    student_id=self.student_id, amount=700000 + index,
                    month=date.today().strftime('%Y-%m'), status='pending', due_date=date.today(),
                )
                for index in range(55)
            ])
            db.session.commit()

        self._login('admin@example.test')
        first_student_page = self.client.get('/students?q=Page&page=1')
        second_student_page = self.client.get('/students?q=Page&page=2')
        self.assertIn(b'Page Learner 00', first_student_page.data)
        self.assertNotIn(b'Page Learner 29', first_student_page.data)
        self.assertIn(b'Page Learner 29', second_student_page.data)
        self.assertIn(b'q=Page', second_student_page.data)

        second_payment_page = self.client.get('/payments?status=pending&page=2')
        self.assertEqual(second_payment_page.status_code, 200)
        self.assertIn(b'status=pending', second_payment_page.data)

    def test_pwa_manifest_worker_scope_and_private_page_policy(self):
        manifest_response = self.client.get('/static/manifest.json')
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.get_json()
        manifest_response.close()
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['scope'], '/')
        self.assertTrue(any(icon['sizes'] == 'any' for icon in manifest['icons']))
        for icon in manifest['icons']:
            icon_response = self.client.get(icon['src'])
            self.assertEqual(icon_response.status_code, 200)
            self.assertEqual(icon_response.mimetype, 'image/svg+xml')
            icon_response.close()

        worker_response = self.client.get('/service-worker.js')
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_response.headers['Service-Worker-Allowed'], '/')
        worker_source = worker_response.get_data(as_text=True)
        self.assertIn("request.mode === 'navigate'", worker_source)
        self.assertIn("url.pathname.startsWith('/static/pwa/')", worker_source)
        self.assertNotIn("caches.put(request, response)", worker_source)
        worker_response.close()
        offline_response = self.client.get('/static/pwa/offline.html')
        self.assertEqual(offline_response.status_code, 200)
        offline_response.close()

        login_response = self.client.get('/login')
        login_page = login_response.get_data(as_text=True)
        self.assertIn('rel="manifest"', login_page)
        self.assertIn("navigator.serviceWorker.register('/service-worker.js'", login_page)
        self.assertIn('id="installAppButton"', login_page)
        login_response.close()

    def test_parent_registration_and_student_search_pages_render(self):
        self._login('admin@example.test')
        response = self.client.get('/students?q=Test')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Test Student', response.data)
        self.assertNotIn(b'Other Student', response.data)
        self.assertEqual(self.client.get('/groups').status_code, 200)
        self.assertEqual(self.client.get('/payments').status_code, 200)
        self.assertEqual(self.client.get('/users').status_code, 200)


if __name__ == '__main__':
    unittest.main()