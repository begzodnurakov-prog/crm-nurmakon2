"""add payment recorder, transfer method, and signed student balances

Revision ID: 7a2c91e5f034
Revises: f483b9c1d672
Create Date: 2026-09-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '7a2c91e5f034'
down_revision = 'f483b9c1d672'
branch_labels = None
depends_on = None


def upgrade():
    is_sqlite = op.get_bind().dialect.name == 'sqlite'
    if is_sqlite:
        op.add_column('payment', sa.Column('_payment_date_value', sa.String(length=10), nullable=True))
        op.execute(
            "UPDATE payment SET _payment_date_value = strftime('%Y-%m-%d', payment_date) "
            'WHERE payment_date IS NOT NULL'
        )

    with op.batch_alter_table('payment') as batch_op:
        batch_op.drop_constraint('ck_payment_type', type_='check')
        batch_op.alter_column(
            'payment_type', existing_type=sa.String(length=10), type_=sa.String(length=20),
        )
        batch_op.alter_column(
            'payment_date', existing_type=sa.Date(), type_=sa.DateTime(timezone=True),
            postgresql_using="payment_date::timestamp AT TIME ZONE 'UTC'",
        )
        batch_op.add_column(sa.Column('recorded_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('is_credit', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.create_foreign_key(
            'fk_payment_recorded_by_id_user_account', 'user_account', ['recorded_by_id'], ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_check_constraint(
            'ck_payment_type', "payment_type IN ('cash', 'card', 'bank_transfer')",
        )

    if is_sqlite:
        op.execute(
            "UPDATE payment SET payment_date = _payment_date_value || ' 00:00:00' "
            'WHERE _payment_date_value IS NOT NULL'
        )
        op.drop_column('payment', '_payment_date_value')

    op.create_index('ix_payment_recorded_by_id', 'payment', ['recorded_by_id'])
    op.execute('UPDATE student SET balance = -balance WHERE balance > 0')


def downgrade():
    connection = op.get_bind()
    transfer_count = connection.execute(sa.text(
        "SELECT COUNT(*) FROM payment WHERE payment_type = 'bank_transfer'"
    )).scalar_one()
    if transfer_count:
        raise RuntimeError('Convert bank-transfer payments before downgrading this revision.')

    op.execute('UPDATE student SET balance = -balance WHERE balance < 0')
    is_sqlite = op.get_bind().dialect.name == 'sqlite'
    if is_sqlite:
        op.add_column('payment', sa.Column('_payment_date_value', sa.String(length=10), nullable=True))
        op.execute(
            "UPDATE payment SET _payment_date_value = strftime('%Y-%m-%d', payment_date) "
            'WHERE payment_date IS NOT NULL'
        )

    op.drop_index('ix_payment_recorded_by_id', table_name='payment')
    with op.batch_alter_table('payment') as batch_op:
        batch_op.drop_constraint('ck_payment_type', type_='check')
        batch_op.drop_constraint('fk_payment_recorded_by_id_user_account', type_='foreignkey')
        batch_op.drop_column('notes')
        batch_op.drop_column('is_credit')
        batch_op.drop_column('recorded_by_id')
        batch_op.alter_column(
            'payment_date', existing_type=sa.DateTime(timezone=True), type_=sa.Date(),
            postgresql_using='payment_date::date',
        )
        batch_op.alter_column(
            'payment_type', existing_type=sa.String(length=20), type_=sa.String(length=10),
        )
        batch_op.create_check_constraint('ck_payment_type', "payment_type IN ('cash', 'card')")

    if is_sqlite:
        op.execute(
            'UPDATE payment SET payment_date = _payment_date_value '
            'WHERE _payment_date_value IS NOT NULL'
        )
        op.drop_column('payment', '_payment_date_value')