"""Initial transactional payment/outbox schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("customers",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(64)))
    op.create_table("orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), sa.ForeignKey("customers.telegram_id"), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="order_positive_amount"),
        sa.CheckConstraint("status IN ('waiting_payment','paid','provisioning','fulfilled',"
                           "'provisioning_failed','cancelled','expired','refunded')", name="order_status"),
        sa.UniqueConstraint("telegram_id", "request_key", name="order_request_key"))
    op.create_table("payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("charge_id", sa.String(256), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "charge_id", name="payment_charge_identity"),
        sa.UniqueConstraint("order_id", name="one_payment_per_order"),
        sa.CheckConstraint("amount > 0", name="payment_positive_amount"))
    op.create_table("outbox_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint("attempts >= 0", name="job_attempts"),
        sa.CheckConstraint("status IN ('pending','running','done','failed')", name="job_status"))
    op.create_index("ix_outbox_jobs_status", "outbox_jobs", ["status"])
    op.create_index("ix_outbox_jobs_available_at", "outbox_jobs", ["available_at"])


def downgrade():
    op.drop_table("outbox_jobs")
    op.drop_table("payments")
    op.drop_table("orders")
    op.drop_table("customers")
