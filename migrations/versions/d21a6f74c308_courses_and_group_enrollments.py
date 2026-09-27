"""add courses, group schedules, and many-to-many student enrollments

Revision ID: d21a6f74c308
Revises: c43b6a12e8f1
Create Date: 2026-09-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd21a6f74c308'
down_revision = 'c43b6a12e8f1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'course',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('price', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
        sa.Column('duration_months', sa.Integer(), server_default='1', nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title'),
    )
    with op.batch_alter_table('learning_group') as batch_op:
        batch_op.add_column(sa.Column('course_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('room_number', sa.String(length=80), server_default='', nullable=False))
        batch_op.add_column(sa.Column('schedule_days', sa.String(length=20), server_default='', nullable=False))
        batch_op.add_column(sa.Column('start_time', sa.Time(), nullable=True))
        batch_op.create_foreign_key(
            'fk_learning_group_course_id_course', 'course', ['course_id'], ['id'], ondelete='SET NULL',
        )
    op.create_index('ix_learning_group_course_id', 'learning_group', ['course_id'])
    op.create_table(
        'student_groups',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['learning_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['student.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('student_id', 'group_id'),
    )

    connection = op.get_bind()
    groups = sa.table(
        'learning_group',
        sa.column('id', sa.Integer()),
        sa.column('course_name', sa.String()),
        sa.column('course_id', sa.Integer()),
    )
    students = sa.table(
        'student',
        sa.column('id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
    )
    enrollments = sa.table(
        'student_groups',
        sa.column('student_id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
    )
    course = sa.table('course', sa.column('id', sa.Integer()), sa.column('title', sa.String()))
    for row in connection.execute(sa.select(groups.c.course_name).distinct()):
        title = (row.course_name or 'General').strip() or 'General'
        existing = connection.execute(sa.select(course.c.id).where(course.c.title == title)).first()
        if existing is None:
            connection.execute(sa.text('INSERT INTO course (title, price, duration_months) VALUES (:title, 0, 1)'), {'title': title})
    for group in connection.execute(sa.select(groups.c.id, groups.c.course_name)):
        title = (group.course_name or 'General').strip() or 'General'
        course_id = connection.execute(sa.select(course.c.id).where(course.c.title == title)).scalar_one()
        connection.execute(groups.update().where(groups.c.id == group.id).values(course_id=course_id))
    for student in connection.execute(sa.select(students.c.id, students.c.group_id).where(students.c.group_id.is_not(None))):
        connection.execute(enrollments.insert().values(student_id=student.id, group_id=student.group_id))


def downgrade():
    op.drop_table('student_groups')
    op.drop_index('ix_learning_group_course_id', table_name='learning_group')
    with op.batch_alter_table('learning_group') as batch_op:
        batch_op.drop_constraint('fk_learning_group_course_id_course', type_='foreignkey')
        batch_op.drop_column('start_time')
        batch_op.drop_column('schedule_days')
        batch_op.drop_column('room_number')
        batch_op.drop_column('course_id')
    op.drop_table('course')