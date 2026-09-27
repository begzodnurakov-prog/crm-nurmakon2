"""add payment collection fields and student balances

Revision ID: c43b6a12e8f1
Revises: 10f5ea802def
Create Date: 2026-09-26 16:00:00.000000

"""
from datetime import date, datetime
from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision = 'c43b6a12e8f1'
down_revision = '10f5ea802def'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('student', sa.Column('balance', sa.Numeric(12, 2), nullable=False, server_default='0'))
    op.add_column('student', sa.Column('payment_status', sa.String(length=20), nullable=False, server_default='paid'))
    op.add_column('payment', sa.Column('payment_type', sa.String(length=10), nullable=False, server_default='cash'))
    op.add_column('payment', sa.Column('payment_date', sa.Date(), nullable=True))
    op.add_column('payment', sa.Column('receipt_number', sa.String(length=40), nullable=True))

    with op.batch_alter_table('student') as batch_op:
        batch_op.create_check_constraint(
            'ck_student_payment_status', "payment_status IN ('paid', 'pending', 'overdue')",
        )
    with op.batch_alter_table('payment') as batch_op:
        batch_op.create_check_constraint('ck_payment_type', "payment_type IN ('cash', 'card')")

    op.create_index('ix_student_payment_status', 'student', ['payment_status'])
    op.create_index('ix_payment_payment_date', 'payment', ['payment_date'])
    op.create_index('ix_payment_receipt_number', 'payment', ['receipt_number'], unique=True)

    connection = op.get_bind()
    payment_table = sa.table(
        'payment',
        sa.column('id', sa.Integer),
        sa.column('student_id', sa.Integer),
        sa.column('amount', sa.Numeric(12, 2)),
        sa.column('status', sa.String(20)),
        sa.column('due_date', sa.Date),
        sa.column('paid_at', sa.DateTime),
        sa.column('payment_date', sa.Date),
    )
    for row in connection.execute(sa.select(
        payment_table.c.id, payment_table.c.paid_at,
    ).where(payment_table.c.paid_at.is_not(None))):
        paid_at = row.paid_at
        if isinstance(paid_at, str):
            paid_at = datetime.fromisoformat(paid_at)
        connection.execute(
            payment_table.update().where(payment_table.c.id == row.id).values(payment_date=paid_at.date())
        )

    student_table = sa.table(
        'student',
        sa.column('id', sa.Integer),
        sa.column('balance', sa.Numeric(12, 2)),
        sa.column('payment_status', sa.String(20)),
    )
    today = date.today()
    for student_row in connection.execute(sa.select(student_table.c.id)):
        pending_rows = connection.execute(sa.select(
            payment_table.c.amount, payment_table.c.due_date,
        ).where(
            payment_table.c.student_id == student_row.id,
            payment_table.c.status == 'pending',
        )).all()
        balance = sum((Decimal(row.amount) for row in pending_rows), Decimal('0.00'))
        overdue = any(row.due_date and row.due_date < today for row in pending_rows)
        status = 'overdue' if overdue else 'pending' if pending_rows else 'paid'
        connection.execute(student_table.update().where(
            student_table.c.id == student_row.id,
        ).values(balance=balance, payment_status=status))


def downgrade():
    op.drop_index('ix_payment_receipt_number', table_name='payment')
    op.drop_index('ix_payment_payment_date', table_name='payment')
    op.drop_index('ix_student_payment_status', table_name='student')
    with op.batch_alter_table('payment') as batch_op:
        batch_op.drop_constraint('ck_payment_type', type_='check')
        batch_op.drop_column('receipt_number')
        batch_op.drop_column('payment_date')
        batch_op.drop_column('payment_type')
    with op.batch_alter_table('student') as batch_op:
        batch_op.drop_constraint('ck_student_payment_status', type_='check')
        batch_op.drop_column('payment_status')
        batch_op.drop_column('balance')