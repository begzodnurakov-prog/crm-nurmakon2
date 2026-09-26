import unittest
from datetime import date, datetime, timedelta

from app import create_app
from extensions import db
from models import Attendance, ClassSession, Group, Inquiry, Notification, Payment, Student, User


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
        response = self.client.post(f'/payments/{payment_id}/mark-paid')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            payment = db.session.get(Payment, payment_id)
            self.assertEqual(payment.status, 'paid')
            self.assertIsNotNone(payment.paid_at)
        dashboard = self.client.get('/')
        self.assertIn(b'data-chart-values="[1, 0, 0]"', dashboard.data)

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