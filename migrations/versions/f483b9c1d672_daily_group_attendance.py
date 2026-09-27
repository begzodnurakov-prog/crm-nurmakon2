"""support daily group attendance and excused status

Revision ID: f483b9c1d672
Revises: d21a6f74c308
Create Date: 2026-09-27 00:00:00.000000

"""
from datetime import date, datetime

from alembic import op
import sqlalchemy as sa


revision = 'f483b9c1d672'
down_revision = 'd21a6f74c308'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendance_record') as batch_op:
        batch_op.add_column(sa.Column('group_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('session_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), server_default='', nullable=False))
        batch_op.alter_column('session_id', existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint('ck_attendance_status', type_='check')
        batch_op.create_check_constraint(
            'ck_attendance_status', "status IN ('present', 'absent', 'late', 'excused')",
        )
        batch_op.create_foreign_key(
            'fk_attendance_record_group_id_learning_group', 'learning_group',
            ['group_id'], ['id'], ondelete='CASCADE',
        )

    connection = op.get_bind()
    attendance = sa.table(
        'attendance_record',
        sa.column('id', sa.Integer()),
        sa.column('session_id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
        sa.column('session_date', sa.Date()),
    )
    class_session = sa.table(
        'class_session',
        sa.column('id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
        sa.column('starts_at', sa.DateTime()),
    )
    rows = connection.execute(
        sa.select(attendance.c.id, class_session.c.group_id, class_session.c.starts_at)
        .select_from(attendance.join(class_session, attendance.c.session_id == class_session.c.id))
    ).all()
    for row in rows:
        starts_at = row.starts_at
        if isinstance(starts_at, str):
            starts_at = datetime.fromisoformat(starts_at)
        session_date = starts_at.date() if isinstance(starts_at, datetime) else date.fromisoformat(str(starts_at)[:10])
        connection.execute(
            attendance.update().where(attendance.c.id == row.id).values(
                group_id=row.group_id, session_date=session_date,
            )
        )

    with op.batch_alter_table('attendance_record') as batch_op:
        batch_op.alter_column('group_id', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('session_date', existing_type=sa.Date(), nullable=False)
    op.create_index('ix_attendance_record_group_id', 'attendance_record', ['group_id'])
    op.create_index('ix_attendance_record_session_date', 'attendance_record', ['session_date'])
    op.create_index(
        'uq_attendance_daily_student_group_date', 'attendance_record',
        ['student_id', 'group_id', 'session_date'], unique=True,
        sqlite_where=sa.text('session_id IS NULL'),
        postgresql_where=sa.text('session_id IS NULL'),
    )


def downgrade():
    connection = op.get_bind()
    attendance = sa.table(
        'attendance_record',
        sa.column('id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
        sa.column('session_id', sa.Integer()),
        sa.column('session_date', sa.Date()),
        sa.column('status', sa.String()),
    )
    class_session = sa.table(
        'class_session',
        sa.column('id', sa.Integer()),
        sa.column('group_id', sa.Integer()),
        sa.column('starts_at', sa.DateTime()),
        sa.column('topic', sa.String()),
        sa.column('created_by_id', sa.Integer()),
    )
    for row in connection.execute(sa.select(
        attendance.c.group_id, attendance.c.session_date,
    ).where(attendance.c.session_id.is_(None)).distinct()):
        result = connection.execute(class_session.insert().values(
            group_id=row.group_id,
            starts_at=datetime.combine(row.session_date, datetime.min.time()),
            topic='Daily attendance (downgrade)',
            created_by_id=None,
        ))
        session_id = result.inserted_primary_key[0] if result.inserted_primary_key else connection.execute(
            sa.select(sa.func.max(class_session.c.id))
        ).scalar_one()
        connection.execute(attendance.update().where(
            attendance.c.group_id == row.group_id,
            attendance.c.session_date == row.session_date,
            attendance.c.session_id.is_(None),
        ).values(session_id=session_id))
    connection.execute(attendance.update().where(
        attendance.c.status == 'excused',
    ).values(status='absent'))
    op.drop_index('uq_attendance_daily_student_group_date', table_name='attendance_record')
    op.drop_index('ix_attendance_record_session_date', table_name='attendance_record')
    op.drop_index('ix_attendance_record_group_id', table_name='attendance_record')
    with op.batch_alter_table('attendance_record') as batch_op:
        batch_op.drop_constraint('fk_attendance_record_group_id_learning_group', type_='foreignkey')
        batch_op.drop_constraint('ck_attendance_status', type_='check')
        batch_op.create_check_constraint(
            'ck_attendance_status', "status IN ('present', 'absent', 'late')",
        )
        batch_op.alter_column('session_id', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('notes')
        batch_op.drop_column('session_date')
        batch_op.drop_column('group_id')